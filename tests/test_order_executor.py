import logging
import pytest
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
    ex = OrderExecutor(client, book, strategy_name="vwap_wave", logger=MagicMock())
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


def test_submit_crypto_uses_market_order_and_virtual_stop_and_target():
    """Crypto: market entry only — stop AND target are engine-virtual.

    The previous behavior (immediate limit TP after entry) was removed
    because Alpaca rejects the second order as a wash trade, leaving the
    broker holding a phantom position once the engine virtual-target
    closed it on its side. Both exits now route through close_position().
    """
    client = MagicMock()
    client.submit_order.return_value = {"id": "ord-2"}
    book = PositionBook()
    ex = OrderExecutor(client, book, strategy_name="vwap_wave", logger=MagicMock())
    decision = RiskDecision(approved=True, qty=0.1, notional=5000)
    sig = _signal(symbol="BTC/USD", side="long")
    pos = ex.submit(sig, decision, asset_class="crypto")
    assert pos is not None
    # Exactly one broker call — the market entry. No follow-up TP.
    assert client.submit_order.call_count == 1
    payload = client.submit_order.call_args.kwargs
    assert payload["symbol"] == "BTC/USD"
    assert payload["order_type"] == "market"
    # Stop and target are tracked in the book; broker has no resting orders.
    assert book.get("BTC/USD").stop_px == 99
    assert book.get("BTC/USD").target_px == 102
    assert book.get("BTC/USD").target_order_id is None


def test_submit_returns_none_when_rejected():
    client = MagicMock()
    book = PositionBook()
    ex = OrderExecutor(client, book, strategy_name="vwap_wave", logger=MagicMock())
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
    ex = OrderExecutor(client, book, strategy_name="vwap_wave", logger=MagicMock())
    decision = RiskDecision(approved=True, qty=10, notional=1000)
    pos = ex.submit(_signal(), decision, asset_class="equity")
    assert pos.stop_order_id == "sl-1"
    assert pos.order_id == "parent-1"


def test_submit_equity_no_legs_keeps_stop_order_id_none():
    client = MagicMock()
    client.submit_bracket_order.return_value = {"id": "parent-2"}   # paper sometimes omits legs
    book = PositionBook()
    ex = OrderExecutor(client, book, strategy_name="vwap_wave", logger=MagicMock())
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
    ex = OrderExecutor(client, book, strategy_name="vwap_wave", logger=MagicMock())

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
    ex = OrderExecutor(client, book, strategy_name="vwap_wave")  # use real logger so caplog captures it
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
    ex = OrderExecutor(client, book, strategy_name="vwap_wave", logger=MagicMock())
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
    ex = OrderExecutor(client, book, strategy_name="vwap_wave", logger=MagicMock())
    decision = RiskDecision(approved=True, qty=0.1, notional=5000)

    ex.submit(_signal(symbol="PLTR"), decision, asset_class="equity")
    # Crypto entry path is unaffected — DTBP only constrains marginable equities.
    pos = ex.submit(_signal(symbol="BTC/USD", side="long"), decision,
                    asset_class="crypto")
    assert pos is not None
    assert client.submit_order.call_count == 1  # market entry only


def test_reset_cycle_clears_dtbp_short_circuit():
    client = MagicMock()
    client.submit_bracket_order.side_effect = [
        InsufficientBuyingPowerError(403, "insufficient day trading buying power"),
        {"id": "ord-next"},
    ]
    book = PositionBook()
    ex = OrderExecutor(client, book, strategy_name="vwap_wave", logger=MagicMock())
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
    from state.position_book import OpenPosition  # noqa: F811 (re-import in function scope)
    book.add(OpenPosition(
        symbol="AMZN", setup="return_to_value", side="long", qty=1,
        entry_px=265.0, stop_px=264.0, target_px=267.0,
        opened_at=datetime(2026, 5, 20, 19, 40, tzinfo=timezone.utc),
        order_id="parent-amzn",
    ))
    book.close("AMZN")
    book.clear_just_exited()  # next cycle starts

    ex = OrderExecutor(client, book, strategy_name="vwap_wave", logger=MagicMock())
    sig = _signal(symbol="AMZN", side="long")
    decision = RiskDecision(approved=True, qty=10, notional=1000)
    pos = ex.submit(sig, decision, asset_class="equity")
    assert pos is not None
    client.submit_bracket_order.assert_called_once()


def test_submit_crypto_short_is_blocked_immediately():
    client = MagicMock()
    book = PositionBook()
    ex = OrderExecutor(client, book, strategy_name="vwap_wave", logger=MagicMock())
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
    ex = OrderExecutor(client, book, strategy_name="vwap_wave", logger=MagicMock())
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


def test_virtual_exit_adopted_crypto_target_submits_close():
    client = MagicMock()
    book = PositionBook()
    # Add an adopted position to the book
    from state.position_book import OpenPosition
    pos = OpenPosition(
        symbol="BTC/USD", setup="adopted", side="long", qty=0.1,
        entry_px=50000.0, stop_px=49000.0, target_px=51000.0,
        opened_at=datetime.now(timezone.utc), order_id="",
        adopted=True
    )
    book.add(pos)
    
    ex = OrderExecutor(client, book, strategy_name="vwap_wave", logger=MagicMock())
    # Generate a target exit action
    from core.position_manager import PositionAction
    action = PositionAction(symbol="BTC/USD", setup="adopted", side="long", qty=0.1, price=51000.0, kind="target")
    
    ex.handle_actions([action], "crypto")
    
    # It must call close_position (which calls submit_order to sell)
    assert client.submit_order.called
    payload = client.submit_order.call_args.kwargs
    assert payload["symbol"] == "BTC/USD"
    assert payload["side"] == "sell"
    # Crypto closes shave the fee-drift margin off the requested qty.
    assert payload["qty"] == pytest.approx(0.1 * (1 - 1e-6), rel=1e-12)


def test_submit_equity_passes_coid_to_bracket_order():
    """Equity submit must mint a role=entry COID and pass it to submit_bracket_order."""
    client = MagicMock()
    client.submit_bracket_order.return_value = {"id": "ord-1"}
    book = PositionBook()
    ex = OrderExecutor(client, book, strategy_name="vwap_wave", logger=MagicMock())
    decision = RiskDecision(approved=True, qty=10, notional=1000)

    pos = ex.submit(_signal(), decision, asset_class="equity")

    assert pos is not None
    assert client.submit_bracket_order.called
    coid = client.submit_bracket_order.call_args.kwargs["client_order_id"]
    assert coid is not None and coid.startswith("aitrader__vwap_wave__price_discovery__AAPL__entry__")
    # Position carries the same COID
    assert pos.client_order_id == coid


def test_submit_crypto_passes_role_entry_coid_to_market_order():
    """Crypto entry mints role=entry on the market order. No TP limit is
    submitted (target is engine-virtual since the wash-trade fix)."""
    client = MagicMock()
    client.submit_order.return_value = {"id": "ord-2"}
    book = PositionBook()
    ex = OrderExecutor(client, book, strategy_name="vwap_wave", logger=MagicMock())
    decision = RiskDecision(approved=True, qty=0.1, notional=5000)

    pos = ex.submit(_signal(symbol="BTC/USD", side="long"), decision, asset_class="crypto")

    assert pos is not None
    assert client.submit_order.call_count == 1
    entry_coid = client.submit_order.call_args.kwargs["client_order_id"]
    assert entry_coid.startswith("aitrader__vwap_wave__price_discovery__BTCUSD__entry__")
    assert pos.client_order_id == entry_coid


def test_close_position_passes_role_exit_coid():
    client = MagicMock()
    client.submit_order.return_value = {"id": "close-1"}
    book = PositionBook()
    ex = OrderExecutor(client, book, strategy_name="vwap_wave", logger=MagicMock())

    result = ex.close_position(symbol="BTCUSD", side="long", qty=0.5)

    assert result == {"id": "close-1"}
    client.submit_order.assert_called_once()
    coid = client.submit_order.call_args.kwargs["client_order_id"]
    # Setup constant "_unknown" is sanitized to "unknown" by the COID format helper.
    assert coid.startswith("aitrader__vwap_wave__unknown__BTCUSD__exit__")
    # close_position is the crypto path; qty is shrunk by the fee-drift margin
    # (DEFAULT_DRIFT_MARGIN = 1e-6) to stay below broker available.
    submitted_qty = client.submit_order.call_args.kwargs["qty"]
    assert submitted_qty == pytest.approx(0.5 * (1 - 1e-6), rel=1e-12)


def test_close_position_insufficient_balance_triggers_qty_reconcile():
    """Alpaca crypto closes return 'insufficient balance for <asset>' when
    the trader-book qty exceeds on-broker qty (fees came out of asset side).
    Must trigger the qty-reconcile fallback, not just log CLOSE_FAILED.
    """
    from broker.alpaca_client import InsufficientBuyingPowerError

    client = MagicMock()
    # First submit (with stale book qty + drift margin) still rejects.
    # Fallback re-fetches broker truth and re-submits at broker_qty * (1 - margin).
    client.submit_order.side_effect = [
        InsufficientBuyingPowerError(
            403, "insufficient balance for SOL "
                 "(requested: 1233.09915901, available: 1230.318126774)",
        ),
        {"id": "close-recovered"},
    ]
    client.get_positions.return_value = [
        {"symbol": "SOLUSD", "qty": "1230.318126774"},
    ]

    book = PositionBook()
    ex = OrderExecutor(client, book, strategy_name="vwap_bands",
                       logger=MagicMock())

    result = ex.close_position(symbol="SOLUSD", side="long", qty=1233.09915901)

    assert result == {"id": "close-recovered"}
    assert client.submit_order.call_count == 2
    # Second call must use broker-reported qty shrunk by the drift margin so
    # any further fee deduction between get_positions() and submit_order()
    # doesn't trigger another rejection.
    second_call = client.submit_order.call_args_list[1].kwargs
    assert second_call["qty"] == pytest.approx(
        1230.318126774 * (1 - 1e-6), rel=1e-12,
    )


def test_close_position_insufficient_balance_no_broker_position_returns_none():
    """If the broker has no matching position, the fallback bails — caller
    sees None instead of an exception."""
    from broker.alpaca_client import InsufficientBuyingPowerError

    client = MagicMock()
    client.submit_order.side_effect = InsufficientBuyingPowerError(
        403, "insufficient balance for SOL",
    )
    client.get_positions.return_value = []

    book = PositionBook()
    ex = OrderExecutor(client, book, strategy_name="vwap_bands",
                       logger=MagicMock())

    result = ex.close_position(symbol="SOLUSD", side="long", qty=1233.0)
    assert result is None


def test_close_position_pepe_dust_drift_succeeds_first_try():
    """The PEPE production failure: requested 25965312924.201588, available
    25965312924.201586171 — a 1.7e-6 unit drift on a ~26B unit position.
    With the 1e-6 fee-drift margin applied, the first submit asks for less
    than available and clears without retry."""
    client = MagicMock()
    client.submit_order.return_value = {"id": "close-pepe"}
    book = PositionBook()
    ex = OrderExecutor(client, book, strategy_name="vwap_wave",
                       logger=MagicMock())

    requested = 25965312924.201588
    result = ex.close_position(symbol="PEPEUSD", side="long", qty=requested)

    assert result == {"id": "close-pepe"}
    assert client.submit_order.call_count == 1
    # The shrunk qty must be strictly less than broker-available so Alpaca
    # accepts on the first submit.
    submitted = client.submit_order.call_args.kwargs["qty"]
    available = 25965312924.201586171
    assert submitted < available


def test_empty_strategy_name_raises():
    with pytest.raises(ValueError, match="non-empty strategy_name"):
        OrderExecutor(MagicMock(), PositionBook(), strategy_name="")


# ---------------------------------------------------------------------------
# Extended-hours (Gap-and-Go) entry path
# ---------------------------------------------------------------------------


def _eh_signal(symbol="AAPL"):
    return SetupSignal(
        setup="gap_and_go", symbol=symbol, side="long",
        entry=200.0, stop=198.0, target=204.0, atr=1.0, level=200.0,
        ts=datetime(2026, 5, 29, 12, 30, tzinfo=timezone.utc),
        notes={"style": "gap_continuation", "extended_hours": True,
               "premarket_high": 199.95, "premarket_low": 197.0},
    )


def test_submit_extended_hours_uses_plain_limit_not_bracket():
    client = MagicMock()
    client.submit_order.return_value = {"id": "eh-1"}
    book = PositionBook()
    ex = OrderExecutor(client, book, strategy_name="gap_and_go", logger=MagicMock())
    decision = RiskDecision(approved=True, qty=10, notional=2000)
    pos = ex.submit(_eh_signal(), decision, asset_class="equity")
    assert pos is not None
    client.submit_bracket_order.assert_not_called()
    client.submit_order.assert_called_once()
    payload = client.submit_order.call_args.kwargs
    assert payload["order_type"] == "limit"
    assert payload["time_in_force"] == "day"
    assert payload["extended_hours"] is True
    assert payload["limit_price"] == 200.0
    assert payload["side"] == "buy"


def test_submit_extended_hours_marks_position_pending_oco_attach():
    client = MagicMock()
    client.submit_order.return_value = {"id": "eh-2"}
    book = PositionBook()
    ex = OrderExecutor(client, book, strategy_name="gap_and_go", logger=MagicMock())
    decision = RiskDecision(approved=True, qty=10, notional=2000)
    pos = ex.submit(_eh_signal(), decision, asset_class="equity")
    assert pos.pending_oco_attach is True
    assert pos.stop_px == 198.0
    assert pos.target_px == 204.0
    assert pos.stop_order_id is None  # no bracket leg yet


def test_submit_regular_equity_does_not_set_pending_oco_attach():
    """The default bracket path must not flip the new flag."""
    client = MagicMock()
    client.submit_bracket_order.return_value = {"id": "ord-1"}
    book = PositionBook()
    ex = OrderExecutor(client, book, strategy_name="orb_vwap", logger=MagicMock())
    decision = RiskDecision(approved=True, qty=10, notional=1000)
    pos = ex.submit(_signal(), decision, asset_class="equity")
    assert pos.pending_oco_attach is False


# ---------------------------------------------------------------------------
# Pre-submit guards — bracket geometry + opposing-position
# ---------------------------------------------------------------------------


def _bad_signal(symbol="AAPL", *, side="long",
                entry=100.0, stop=99.0, target=102.0):
    return SetupSignal(
        setup="price_discovery", symbol=symbol, side=side,
        entry=entry, stop=stop, target=target, atr=1.0, level=entry,
        ts=datetime(2026, 5, 14, 14, 0, tzinfo=timezone.utc),
    )


@pytest.mark.parametrize(
    "side,entry,stop,target,fragment",
    [
        # Long: target at-or-below entry → must be >= entry+0.01
        ("long", 100.0, 99.0, 100.0, "long target must be >="),
        ("long", 100.0, 99.0, 99.50, "long target must be >="),
        # Long: target only $0.005 above entry (less than tick) → reject
        ("long", 100.0, 99.0, 100.005, "long target must be >="),
        # Long: stop at-or-above entry → must be <= entry-0.01
        ("long", 100.0, 100.0, 102.0, "long stop must be <="),
        ("long", 100.0, 100.005, 102.0, "long stop must be <="),
        # Short: target at-or-above entry → must be <= entry-0.01
        ("short", 100.0, 101.0, 100.0, "short target must be <="),
        ("short", 100.0, 101.0, 100.005, "short target must be <="),
        # Short: stop at-or-below entry → must be >= entry+0.01
        ("short", 100.0, 100.0, 99.0, "short stop must be >="),
        ("short", 100.0, 99.995, 99.0, "short stop must be >="),
    ],
)
def test_submit_rejects_invalid_equity_bracket_geometry(
    side, entry, stop, target, fragment,
):
    """Setup bug → reject locally before any Alpaca call. Replaces the
    422 stack trace operators were seeing with a clear log line."""
    client = MagicMock()
    book = PositionBook()
    log = MagicMock()
    ex = OrderExecutor(client, book, strategy_name="vwap_wave", logger=log)
    decision = RiskDecision(approved=True, qty=10, notional=1000)
    sig = _bad_signal(side=side, entry=entry, stop=stop, target=target)

    pos = ex.submit(sig, decision, asset_class="equity")

    assert pos is None
    client.submit_bracket_order.assert_not_called()
    client.submit_order.assert_not_called()
    # Surface the reject reason via the warning log so operators see it.
    assert log.warning.called
    args = log.warning.call_args.args
    msg = args[0] % args[1:]
    assert "ORDER_SKIPPED_INVALID_LEVELS" in msg
    assert fragment in msg


def test_submit_accepts_subdollar_tick_geometry():
    """Sub-$1 prices use a $0.0001 tick. A target $0.0001 above a $0.50
    entry must pass; a target $0.00005 above must fail."""
    client = MagicMock()
    client.submit_bracket_order.return_value = {"id": "sub-1"}
    book = PositionBook()
    ex = OrderExecutor(client, book, strategy_name="vwap_wave", logger=MagicMock())
    decision = RiskDecision(approved=True, qty=100, notional=50)

    ok_sig = _bad_signal(side="long", entry=0.50, stop=0.4999, target=0.5001)
    pos = ex.submit(ok_sig, decision, asset_class="equity")
    assert pos is not None

    # Reset between calls — book.add already inserted the first one.
    book = PositionBook()
    ex = OrderExecutor(client, book, strategy_name="vwap_wave", logger=MagicMock())
    bad_sig = _bad_signal(side="long", entry=0.50, stop=0.4999, target=0.50005)
    pos = ex.submit(bad_sig, decision, asset_class="equity")
    assert pos is None


def test_submit_skips_when_book_already_has_opposing_position():
    """Phantom long limit lingers in the book until the reconciler resolves
    it. A new short signal must not be submitted to the broker, which
    would 403 with `cannot open a short sell while a long buy order is
    open`."""
    client = MagicMock()
    book = PositionBook()

    from state.position_book import OpenPosition
    book.add(OpenPosition(
        symbol="UBER", setup="rsi_reversion", side="long", qty=2,
        entry_px=73.87, stop_px=72.39, target_px=76.09,
        opened_at=datetime(2026, 6, 1, 14, 0, tzinfo=timezone.utc),
        order_id="alp-uber-pending",
    ))

    log = MagicMock()
    ex = OrderExecutor(client, book, strategy_name="rsi_equity", logger=log)
    decision = RiskDecision(approved=True, qty=2, notional=147)
    sig = _bad_signal(symbol="UBER", side="short",
                      entry=73.87, stop=74.50, target=72.50)

    pos = ex.submit(sig, decision, asset_class="equity")

    assert pos is None
    client.submit_bracket_order.assert_not_called()
    msg = log.info.call_args.args[0] % log.info.call_args.args[1:]
    assert "ORDER_SKIPPED_OPPOSING_OPEN_ORDER" in msg


def test_submit_skips_when_book_has_same_side_same_setup():
    """Double-entry on the same (symbol, setup) should never reach
    book.add (which raises) — the new guard catches it as a clean log."""
    client = MagicMock()
    book = PositionBook()

    from state.position_book import OpenPosition
    book.add(OpenPosition(
        symbol="AAPL", setup="price_discovery", side="long", qty=10,
        entry_px=100.0, stop_px=99.0, target_px=102.0,
        opened_at=datetime(2026, 5, 14, 14, 0, tzinfo=timezone.utc),
        order_id="o-existing",
    ))

    ex = OrderExecutor(client, book, strategy_name="vwap_wave", logger=MagicMock())
    decision = RiskDecision(approved=True, qty=10, notional=1000)
    pos = ex.submit(_signal(), decision, asset_class="equity")

    assert pos is None
    client.submit_bracket_order.assert_not_called()


def test_submit_crypto_skips_geometry_check():
    """The validator only fires for equity bracket entries. Crypto market
    entries don't go through Alpaca's bracket validator."""
    client = MagicMock()
    client.submit_order.return_value = {"id": "crypto-1"}
    book = PositionBook()
    ex = OrderExecutor(client, book, strategy_name="ib_crypto", logger=MagicMock())
    decision = RiskDecision(approved=True, qty=0.1, notional=5000)
    # Target equals entry — would fail the equity validator, must NOT
    # block a crypto submit (engine-virtual exits handle this).
    sig = _bad_signal(symbol="BTC/USD", side="long",
                      entry=50000.0, stop=49500.0, target=50000.0)

    pos = ex.submit(sig, decision, asset_class="crypto")

    assert pos is not None
    client.submit_order.assert_called_once()


def test_submit_extended_hours_equity_skips_geometry_check():
    """Pre-market limit entries don't carry the bracket — OCO is attached
    after the open. Geometry validation must therefore NOT block an
    extended_hours signal even if its declared target/stop are degenerate
    relative to entry (post_open_attach reads its own levels off the
    OpenPosition later)."""
    client = MagicMock()
    client.submit_order.return_value = {"id": "eh-1"}
    book = PositionBook()
    ex = OrderExecutor(client, book, strategy_name="gap_and_go", logger=MagicMock())
    decision = RiskDecision(approved=True, qty=10, notional=2000)
    sig = SetupSignal(
        setup="gap_and_go", symbol="UBER", side="long",
        entry=200.0, stop=199.0, target=200.0,  # would fail equity bracket validator
        atr=1.0, level=200.0,
        ts=datetime(2026, 5, 14, 14, 0, tzinfo=timezone.utc),
        notes={"extended_hours": True},
    )

    pos = ex.submit(sig, decision, asset_class="equity")
    assert pos is not None
    client.submit_order.assert_called_once()
    client.submit_bracket_order.assert_not_called()
