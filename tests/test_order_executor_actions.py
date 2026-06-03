from unittest.mock import MagicMock

from datetime import datetime, timezone

import pytest

from broker.alpaca_client import OrderRejectedError
from broker.order_executor import OrderExecutor
from core.position_manager import PositionAction
from state.position_book import OpenPosition, PositionBook


def _make_executor():
    client = MagicMock()
    book = PositionBook()
    ex = OrderExecutor(client, book, strategy_name="vwap_wave", logger=MagicMock())
    return ex, client


def _action(kind, side="long", symbol="AAPL", qty=10, price=99.0):
    return PositionAction(symbol=symbol, setup="adopted", kind=kind, price=price, qty=qty, side=side)


# ---- crypto: PositionManager owns stop/target/time_stop -------------------

def test_crypto_stop_submits_market_close_long():
    ex, client = _make_executor()
    ex.handle_actions([_action("stop", side="long", symbol="BTC/USD", qty=0.1)],
                      asset_class="crypto", parent_order_id="parent-1")
    client.submit_order.assert_called_once()
    kwargs = client.submit_order.call_args.kwargs
    assert kwargs["symbol"] == "BTC/USD"
    assert kwargs["side"] == "sell"
    # Crypto closes shave the fee-drift safety margin off the requested qty.
    assert kwargs["qty"] == pytest.approx(0.1 * (1 - 1e-6), rel=1e-12)
    assert kwargs["order_type"] == "market"


def test_crypto_target_submits_market_close():
    """Crypto target is engine-virtual — must issue a market close on the broker.

    Previously this path skipped the broker close on the assumption that a
    resting limit TP would fill. That submission was rejected by Alpaca as a
    wash trade, so positions never closed broker-side and ate buying power.
    Now stop/target/time_stop all market-close uniformly.
    """
    ex, client = _make_executor()
    ex.handle_actions([_action("target", side="short", symbol="ETH/USD", qty=0.5)],
                      asset_class="crypto", parent_order_id="parent-2")
    client.submit_order.assert_called_once()
    kwargs = client.submit_order.call_args.kwargs
    assert kwargs["symbol"] == "ETH/USD"
    assert kwargs["side"] == "buy"  # closes a short
    assert kwargs["qty"] == pytest.approx(0.5 * (1 - 1e-6), rel=1e-12)
    assert kwargs["order_type"] == "market"
    client.cancel_order.assert_not_called()


def test_crypto_target_cancels_legacy_target_order_id_before_close():
    """Adopted positions from before the wash-trade fix may still carry a
    target_order_id — cancel it first so the broker side is tidy."""
    ex, client = _make_executor()
    ex.book.add(OpenPosition(
        symbol="BTC/USD", setup="adopted", side="long", qty=0.1,
        entry_px=50000, stop_px=49000, target_px=52000,
        opened_at=datetime(2026, 5, 29, 14, 0, tzinfo=timezone.utc),
        order_id="entry-1", target_order_id="legacy-tp-1",
    ))
    ex.handle_actions([_action("target", side="long", symbol="BTC/USD", qty=0.1)],
                      asset_class="crypto", parent_order_id=None)
    client.cancel_order.assert_called_once_with("legacy-tp-1")
    client.submit_order.assert_called_once()  # market close after cancel


def test_crypto_time_stop_submits_market_close():
    ex, client = _make_executor()
    ex.handle_actions([_action("time_stop", side="long", symbol="BTC/USD", qty=0.1)],
                      asset_class="crypto", parent_order_id="parent-3")
    client.submit_order.assert_called_once()
    client.cancel_order.assert_not_called()


# ---- equity: broker bracket owns stop/target ------------------------------

def test_equity_stop_is_noop():
    ex, client = _make_executor()
    ex.handle_actions([_action("stop")], asset_class="equity", parent_order_id="parent-4")
    client.submit_order.assert_not_called()
    client.cancel_order.assert_not_called()


def test_equity_target_is_noop():
    ex, client = _make_executor()
    ex.handle_actions([_action("target")], asset_class="equity", parent_order_id="parent-5")
    client.submit_order.assert_not_called()
    client.cancel_order.assert_not_called()


def test_equity_time_stop_cancels_parent_then_market_close():
    ex, client = _make_executor()
    ex.handle_actions([_action("time_stop", side="long", qty=10)],
                      asset_class="equity", parent_order_id="parent-6")
    client.cancel_order.assert_called_once_with("parent-6")
    client.submit_order.assert_called_once()
    kwargs = client.submit_order.call_args.kwargs
    assert kwargs["side"] == "sell"
    assert kwargs["qty"] == 10


# ---- breakeven: state-only on both classes --------------------------------

def test_crypto_breakeven_does_not_submit_orders():
    ex, client = _make_executor()
    ex.handle_actions([_action("breakeven", symbol="BTC/USD", qty=0.1)],
                      asset_class="crypto", parent_order_id="p")
    client.submit_order.assert_not_called()
    client.cancel_order.assert_not_called()
    client.replace_order.assert_not_called()


def _seed_open_position(book, *, symbol="AAPL", side="long", entry=100.0,
                        stop_order_id="sl-1", parent_order_id="parent-1"):
    book.add(OpenPosition(
        symbol=symbol, setup="adopted", side=side, qty=10,
        entry_px=entry, stop_px=entry - 1.0, target_px=entry + 2.0,
        opened_at=datetime(2026, 5, 14, 14, 0, tzinfo=timezone.utc),
        order_id=parent_order_id, stop_order_id=stop_order_id,
    ))


def test_equity_breakeven_replaces_stop_leg():
    ex, client = _make_executor()
    _seed_open_position(ex.book, stop_order_id="sl-1")
    ex.handle_actions([_action("breakeven", price=100.0)],
                      asset_class="equity", parent_order_id="parent-1")
    client.replace_order.assert_called_once_with("sl-1", stop_price=100.0)
    client.submit_order.assert_not_called()
    client.cancel_order.assert_not_called()


def test_equity_breakeven_logs_only_when_stop_leg_id_missing():
    ex, client = _make_executor()
    _seed_open_position(ex.book, stop_order_id=None)
    ex.handle_actions([_action("breakeven", price=100.0)],
                      asset_class="equity", parent_order_id="parent-1")
    client.replace_order.assert_not_called()
    client.submit_order.assert_not_called()
    client.cancel_order.assert_not_called()


# ---- breakeven: benign Alpaca rejections become warnings, not errors -------

def _last_error_calls(logger_mock):
    return [c.args[0] for c in logger_mock.error.call_args_list]


def _last_warning_calls(logger_mock):
    return [c.args[0] for c in logger_mock.warning.call_args_list]


def test_breakeven_long_too_close_to_quote_logs_warning_not_error():
    ex, client = _make_executor()
    _seed_open_position(ex.book, stop_order_id="sl-1")
    client.replace_order.side_effect = OrderRejectedError(
        "stop_loss.stop_price must be <= base_price - 0.01")
    ex.handle_actions([_action("breakeven", price=100.0)],
                      asset_class="equity", parent_order_id="parent-1")
    assert not _last_error_calls(ex.logger), "benign rejection must not log ERROR"
    assert any("BREAKEVEN_SKIPPED" in s for s in _last_warning_calls(ex.logger))


def test_breakeven_short_too_close_to_quote_logs_warning_not_error():
    ex, client = _make_executor()
    _seed_open_position(ex.book, side="short", stop_order_id="sl-1")
    client.replace_order.side_effect = OrderRejectedError(
        "stop_loss.stop_price must be >= base_price + 0.01")
    ex.handle_actions([_action("breakeven", side="short", price=100.0)],
                      asset_class="equity", parent_order_id="parent-1")
    assert not _last_error_calls(ex.logger)
    assert any("BREAKEVEN_SKIPPED" in s for s in _last_warning_calls(ex.logger))


def test_breakeven_order_already_closed_logs_warning_not_error():
    ex, client = _make_executor()
    _seed_open_position(ex.book, stop_order_id="sl-1")
    client.replace_order.side_effect = OrderRejectedError("order is not open")
    ex.handle_actions([_action("breakeven", price=100.0)],
                      asset_class="equity", parent_order_id="parent-1")
    assert not _last_error_calls(ex.logger)
    assert any("BREAKEVEN_SKIPPED" in s for s in _last_warning_calls(ex.logger))


def test_breakeven_unexpected_exception_still_logs_error():
    ex, client = _make_executor()
    _seed_open_position(ex.book, stop_order_id="sl-1")
    client.replace_order.side_effect = RuntimeError("network exploded")
    ex.handle_actions([_action("breakeven", price=100.0)],
                      asset_class="equity", parent_order_id="parent-1")
    assert any("BREAKEVEN_REPLACE_FAILED" in s for s in _last_error_calls(ex.logger))


# ---- breakeven idempotency: flip breakeven_moved on success and benign reject

def test_breakeven_replace_success_sets_breakeven_moved():
    """After a successful replace_order, breakeven_moved must be True so
    PositionManager._check_position doesn't re-emit the breakeven action
    on the next bar."""
    ex, client = _make_executor()
    _seed_open_position(ex.book, stop_order_id="sl-1")
    pos_before = ex.book.get("AAPL", "adopted")
    assert pos_before.breakeven_moved is False  # sanity

    ex.handle_actions([_action("breakeven", price=100.0)],
                      asset_class="equity", parent_order_id="parent-1")

    pos_after = ex.book.get("AAPL", "adopted")
    assert pos_after.breakeven_moved is True


def test_breakeven_already_replaced_sets_breakeven_moved():
    """Today's COIN log: BREAKEVEN_REPLACE_FAILED ... order already replaced
    looped every cycle because breakeven_moved didn't flip on the benign
    rejection path. Fix: any benign-fragment rejection (broker has already
    moved the leg, or stop is too close, or order is closed) marks the
    move as done so the engine stops retrying."""
    ex, client = _make_executor()
    _seed_open_position(ex.book, stop_order_id="sl-1")
    client.replace_order.side_effect = OrderRejectedError(
        "order already replaced"
    )

    ex.handle_actions([_action("breakeven", price=100.0)],
                      asset_class="equity", parent_order_id="parent-1")

    pos_after = ex.book.get("AAPL", "adopted")
    assert pos_after.breakeven_moved is True
    # Confirm we logged BREAKEVEN_SKIPPED, not BREAKEVEN_REPLACED
    assert any("BREAKEVEN_SKIPPED" in s for s in _last_warning_calls(ex.logger))


def test_breakeven_unexpected_exception_does_not_set_breakeven_moved():
    """If replace_order fails with a non-benign exception, leave
    breakeven_moved=False so the next cycle retries (matches existing
    BREAKEVEN_REPLACE_FAILED error semantics)."""
    ex, client = _make_executor()
    _seed_open_position(ex.book, stop_order_id="sl-1")
    client.replace_order.side_effect = RuntimeError("network exploded")

    ex.handle_actions([_action("breakeven", price=100.0)],
                      asset_class="equity", parent_order_id="parent-1")

    pos_after = ex.book.get("AAPL", "adopted")
    assert pos_after.breakeven_moved is False
