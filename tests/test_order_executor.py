import logging
from datetime import datetime, timezone
from unittest.mock import MagicMock
from broker.alpaca_client import InsufficientBuyingPowerError
from broker.order_executor import OrderExecutor
from strategies.base_setup import SetupSignal
from state.position_book import PositionBook
from risk.manager import RiskDecision


def _signal(symbol="AAPL", side="long"):
    return SetupSignal(setup="price_discovery", symbol=symbol, side=side,
                       entry=100, stop=99, target=102, atr=1.0, level=100,
                       ts=datetime(2026, 5, 14, 14, 0, tzinfo=timezone.utc))


def test_submit_equity_uses_bracket_order():
    client = MagicMock()
    client.submit_bracket_order.return_value = {"id": "ord-1"}
    book = PositionBook()
    ex = OrderExecutor(client, book, logger=MagicMock())
    decision = RiskDecision(approved=True, qty=10, notional=1000)
    pos = ex.submit(_signal(), decision, asset_class="equity")
    assert pos is not None
    assert pos.symbol == "AAPL"
    assert client.submit_bracket_order.called
    payload = client.submit_bracket_order.call_args.kwargs
    assert payload["side"] == "buy"
    assert payload["symbol"] == "AAPL"
    assert payload["stop_loss"] == 99
    assert payload["take_profit"] == 102


def test_submit_crypto_uses_market_order_and_virtual_stop():
    client = MagicMock()
    client.submit_order.return_value = {"id": "ord-2"}
    book = PositionBook()
    ex = OrderExecutor(client, book, logger=MagicMock())
    decision = RiskDecision(approved=True, qty=0.1, notional=5000)
    sig = _signal(symbol="BTC/USD", side="long")
    pos = ex.submit(sig, decision, asset_class="crypto")
    assert pos is not None
    assert client.submit_order.call_count == 2  # market entry + limit TP
    first_call = client.submit_order.call_args_list[0]
    payload = first_call.kwargs
    assert payload["symbol"] == "BTC/USD"
    assert payload["order_type"] == "market"
    # Virtual stop is tracked in the book
    assert book.get("BTC/USD").stop_px == 99


def test_submit_returns_none_when_rejected():
    client = MagicMock()
    book = PositionBook()
    ex = OrderExecutor(client, book, logger=MagicMock())
    decision = RiskDecision.reject("denied")
    pos = ex.submit(_signal(), decision, asset_class="equity")
    assert pos is None
    client.submit_bracket_order.assert_not_called()
    client.submit_order.assert_not_called()


def test_submit_equity_captures_stop_leg_id():
    client = MagicMock()
    client.submit_bracket_order.return_value = {
        "id": "parent-1",
        "legs": [
            {"id": "tp-1", "type": "limit", "limit_price": 102},
            {"id": "sl-1", "type": "stop", "stop_price": 99},
        ],
    }
    book = PositionBook()
    ex = OrderExecutor(client, book, logger=MagicMock())
    decision = RiskDecision(approved=True, qty=10, notional=1000)
    pos = ex.submit(_signal(), decision, asset_class="equity")
    assert pos.stop_order_id == "sl-1"
    assert pos.order_id == "parent-1"


def test_submit_equity_no_legs_keeps_stop_order_id_none():
    client = MagicMock()
    client.submit_bracket_order.return_value = {"id": "parent-2"}   # paper sometimes omits legs
    book = PositionBook()
    ex = OrderExecutor(client, book, logger=MagicMock())
    decision = RiskDecision(approved=True, qty=10, notional=1000)
    pos = ex.submit(_signal(), decision, asset_class="equity")
    assert pos.stop_order_id is None


def test_submit_skips_when_symbol_just_exited_this_cycle():
    """Symbol whose bracket exited earlier in the same cycle: skip re-entry.

    Alpaca rejects bracket entries while a closing order is still settling,
    and re-entering on the same bar that just stopped us out is rarely the
    intended behavior anyway.
    """
    client = MagicMock()
    book = PositionBook()
    ex = OrderExecutor(client, book, logger=MagicMock())

    # Simulate a same-cycle bracket exit via the public API.
    from state.position_book import OpenPosition
    book.add(OpenPosition(
        symbol="AMZN", setup="return_to_value", side="long", qty=1,
        entry_px=265.0, stop_px=264.0, target_px=267.0,
        opened_at=datetime(2026, 5, 20, 19, 40, tzinfo=timezone.utc),
        order_id="parent-amzn",
    ))
    book.close("AMZN")  # bracket stop fired this cycle

    sig = _signal(symbol="AMZN", side="long")
    decision = RiskDecision(approved=True, qty=10, notional=1000)
    pos = ex.submit(sig, decision, asset_class="equity")
    assert pos is None
    client.submit_bracket_order.assert_not_called()
    client.submit_order.assert_not_called()


def test_submit_logs_dtbp_rejection_at_warning_without_stack_trace(caplog):
    client = MagicMock()
    client.submit_bracket_order.side_effect = InsufficientBuyingPowerError(
        403, "insufficient day trading buying power"
    )
    book = PositionBook()
    ex = OrderExecutor(client, book)  # use real logger so caplog captures it
    decision = RiskDecision(approved=True, qty=10, notional=1000)
    with caplog.at_level(logging.WARNING, logger="vwap_wave.executor"):
        pos = ex.submit(_signal(symbol="PLTR"), decision, asset_class="equity")
    assert pos is None
    rec = next(r for r in caplog.records if "ORDER_REJECTED_DTBP" in r.getMessage())
    assert rec.levelno == logging.WARNING
    assert rec.exc_info is None
    msg = rec.getMessage()
    assert "PLTR" in msg
    assert "qty=10" in msg


def test_dtbp_rejection_short_circuits_subsequent_equity_submits_in_same_cycle():
    client = MagicMock()
    client.submit_bracket_order.side_effect = InsufficientBuyingPowerError(
        403, "insufficient day trading buying power"
    )
    book = PositionBook()
    ex = OrderExecutor(client, book, logger=MagicMock())
    decision = RiskDecision(approved=True, qty=10, notional=1000)

    # First submit triggers the broker call and gets rejected.
    assert ex.submit(_signal(symbol="PLTR"), decision, asset_class="equity") is None
    assert client.submit_bracket_order.call_count == 1

    # Subsequent equity submit in the same cycle must NOT call the broker.
    assert ex.submit(_signal(symbol="AAPL"), decision, asset_class="equity") is None
    assert client.submit_bracket_order.call_count == 1


def test_dtbp_short_circuit_does_not_block_crypto_submits():
    client = MagicMock()
    client.submit_bracket_order.side_effect = InsufficientBuyingPowerError(
        403, "insufficient day trading buying power"
    )
    client.submit_order.return_value = {"id": "ord-c"}
    book = PositionBook()
    ex = OrderExecutor(client, book, logger=MagicMock())
    decision = RiskDecision(approved=True, qty=0.1, notional=5000)

    ex.submit(_signal(symbol="PLTR"), decision, asset_class="equity")
    # Crypto entry path is unaffected — DTBP only constrains marginable equities.
    pos = ex.submit(_signal(symbol="BTC/USD", side="long"), decision,
                    asset_class="crypto")
    assert pos is not None
    assert client.submit_order.call_count == 2  # market entry + limit TP


def test_reset_cycle_clears_dtbp_short_circuit():
    client = MagicMock()
    client.submit_bracket_order.side_effect = [
        InsufficientBuyingPowerError(403, "insufficient day trading buying power"),
        {"id": "ord-next"},
    ]
    book = PositionBook()
    ex = OrderExecutor(client, book, logger=MagicMock())
    decision = RiskDecision(approved=True, qty=10, notional=1000)

    assert ex.submit(_signal(symbol="PLTR"), decision, asset_class="equity") is None
    # Same-cycle short-circuit
    assert ex.submit(_signal(symbol="AAPL"), decision, asset_class="equity") is None
    assert client.submit_bracket_order.call_count == 1

    ex.reset_cycle()
    pos = ex.submit(_signal(symbol="AAPL"), decision, asset_class="equity")
    assert pos is not None
    assert client.submit_bracket_order.call_count == 2


def test_submit_proceeds_after_just_exited_cleared():
    client = MagicMock()
    client.submit_bracket_order.return_value = {"id": "ord-x"}
    book = PositionBook()
    from state.position_book import OpenPosition
    book.add(OpenPosition(
        symbol="AMZN", setup="return_to_value", side="long", qty=1,
        entry_px=265.0, stop_px=264.0, target_px=267.0,
        opened_at=datetime(2026, 5, 20, 19, 40, tzinfo=timezone.utc),
        order_id="parent-amzn",
    ))
    book.close("AMZN")
    book.clear_just_exited()  # next cycle starts

    ex = OrderExecutor(client, book, logger=MagicMock())
    sig = _signal(symbol="AMZN", side="long")
    decision = RiskDecision(approved=True, qty=10, notional=1000)
    pos = ex.submit(sig, decision, asset_class="equity")
    assert pos is not None
    client.submit_bracket_order.assert_called_once()


def test_submit_crypto_short_is_blocked_immediately():
    client = MagicMock()
    book = PositionBook()
    ex = OrderExecutor(client, book, logger=MagicMock())
    decision = RiskDecision(approved=True, qty=0.1, notional=5000)

    # Short crypto must return None and NOT call the broker
    sig = _signal(symbol="BTC/USD", side="short")
    pos = ex.submit(sig, decision, asset_class="crypto")
    assert pos is None
    client.submit_order.assert_not_called()


def test_crypto_insufficient_buying_power_does_not_trigger_dtbp_exhaustion():
    client = MagicMock()
    client.submit_order.side_effect = InsufficientBuyingPowerError(
        403, "insufficient balance for USD"
    )
    book = PositionBook()
    ex = OrderExecutor(client, book, logger=MagicMock())
    decision = RiskDecision(approved=True, qty=0.1, notional=5000)

    # First submit triggers a crypto long which gets rejected due to insufficient balance
    sig_crypto = _signal(symbol="BTC/USD", side="long")
    assert ex.submit(sig_crypto, decision, asset_class="crypto") is None
    assert client.submit_order.call_count == 1
    assert ex._dtbp_exhausted is False  # Must not set DTBP exhausted for crypto rejections

    # Subsequent equity submit in the same cycle must STILL call the broker
    client.submit_bracket_order.return_value = {"id": "ord-eq"}
    sig_equity = _signal(symbol="AAPL", side="long")
    pos = ex.submit(sig_equity, RiskDecision(approved=True, qty=10, notional=1000), asset_class="equity")
    assert pos is not None
    client.submit_bracket_order.assert_called_once()

