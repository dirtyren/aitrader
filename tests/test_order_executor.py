from datetime import datetime, timezone
from unittest.mock import MagicMock
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
    client.submit_order.assert_called_once()
    payload = client.submit_order.call_args.kwargs
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
