from unittest.mock import MagicMock

from datetime import datetime, timezone

from broker.order_executor import OrderExecutor
from core.position_manager import PositionAction
from state.position_book import OpenPosition, PositionBook


def _make_executor():
    client = MagicMock()
    book = PositionBook()
    ex = OrderExecutor(client, book, logger=MagicMock())
    return ex, client


def _action(kind, side="long", symbol="AAPL", qty=10, price=99.0):
    return PositionAction(symbol=symbol, kind=kind, price=price, qty=qty, side=side)


# ---- crypto: PositionManager owns stop/target/time_stop -------------------

def test_crypto_stop_submits_market_close_long():
    ex, client = _make_executor()
    ex.handle_actions([_action("stop", side="long", symbol="BTC/USD", qty=0.1)],
                      asset_class="crypto", parent_order_id="parent-1")
    client.submit_order.assert_called_once()
    kwargs = client.submit_order.call_args.kwargs
    assert kwargs["symbol"] == "BTC/USD"
    assert kwargs["side"] == "sell"
    assert kwargs["qty"] == 0.1
    assert kwargs["order_type"] == "market"


def test_crypto_target_submits_market_close_short():
    ex, client = _make_executor()
    ex.handle_actions([_action("target", side="short", symbol="ETH/USD", qty=0.5)],
                      asset_class="crypto", parent_order_id="parent-2")
    client.submit_order.assert_called_once()
    assert client.submit_order.call_args.kwargs["side"] == "buy"
    client.cancel_order.assert_not_called()


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
        symbol=symbol, setup="price_discovery", side=side, qty=10,
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
