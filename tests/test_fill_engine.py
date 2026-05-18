from datetime import datetime, timezone

from backtest.fill_engine import PendingOrder, SimulatedFillEngine
from core.bar import Bar
from state.position_book import PositionBook


def _bar(ts, o, h, l, c):
    return Bar(symbol="AAPL", ts=ts, open=o, high=h, low=l, close=c, volume=100)


def _order(symbol="AAPL", side="buy", qty=10, order_type="limit",
           limit_price=100.0, stop_price=99.0, target_price=102.0,
           asset_class="equity"):
    return PendingOrder(symbol=symbol, side=side, qty=qty, order_type=order_type,
                        limit_price=limit_price, stop_price=stop_price,
                        target_price=target_price, asset_class=asset_class,
                        setup="x",
                        ts=datetime(2026, 5, 14, 14, 0, tzinfo=timezone.utc))


def test_limit_fills_when_bar_range_touches():
    fill = SimulatedFillEngine(slippage_bps_by_class={"equity": 0.0, "crypto": 0.0})
    fill.submit(_order(limit_price=100.0))
    book = PositionBook()
    bar = _bar(datetime(2026, 5, 14, 14, 5, tzinfo=timezone.utc), 100.5, 101.0, 99.5, 100.8)
    fills = fill.process_bar("AAPL", bar, book)
    assert len(fills) == 1
    pos = book.get("AAPL")
    assert pos is not None
    assert pos.entry_px == 100.0


def test_limit_skipped_when_bar_misses():
    fill = SimulatedFillEngine(slippage_bps_by_class={"equity": 0.0, "crypto": 0.0})
    fill.submit(_order(limit_price=99.5))
    book = PositionBook()
    bar = _bar(datetime(2026, 5, 14, 14, 5, tzinfo=timezone.utc), 100.5, 101.0, 100.0, 100.8)
    fills = fill.process_bar("AAPL", bar, book)
    assert fills == []
    assert book.get("AAPL") is None
    # missed orders should remain pending for the next bar
    assert len(fill.pending) == 1


def test_market_order_fills_at_open_with_slippage():
    fill = SimulatedFillEngine(slippage_bps_by_class={"equity": 10.0, "crypto": 0.0})
    fill.submit(_order(order_type="market", limit_price=None))
    book = PositionBook()
    bar = _bar(datetime(2026, 5, 14, 14, 5, tzinfo=timezone.utc), 100.0, 101.0, 99.5, 100.5)
    fill.process_bar("AAPL", bar, book)
    pos = book.get("AAPL")
    # 10 bps slippage on a buy = 100.0 * 1.001 = 100.1
    assert abs(pos.entry_px - 100.1) < 1e-9


def test_market_sell_fills_at_open_with_negative_slippage():
    fill = SimulatedFillEngine(slippage_bps_by_class={"equity": 10.0, "crypto": 0.0})
    fill.submit(_order(side="sell", order_type="market", limit_price=None,
                       stop_price=101.0, target_price=98.0))
    book = PositionBook()
    bar = _bar(datetime(2026, 5, 14, 14, 5, tzinfo=timezone.utc), 100.0, 101.0, 99.5, 100.5)
    fill.process_bar("AAPL", bar, book)
    pos = book.get("AAPL")
    # 10 bps slippage on a sell = 100.0 * 0.999 = 99.9
    assert pos.side == "short"
    assert abs(pos.entry_px - 99.9) < 1e-9


def test_stop_order_fills_when_high_breaches_trigger():
    fill = SimulatedFillEngine(slippage_bps_by_class={"equity": 0.0, "crypto": 0.0})
    fill.submit(_order(order_type="stop", limit_price=None, stop_price=100.5))
    book = PositionBook()
    # bar opens below trigger then rallies past it; fill at max(trigger, open)
    bar = _bar(datetime(2026, 5, 14, 14, 5, tzinfo=timezone.utc), 100.0, 101.0, 99.5, 100.8)
    fill.process_bar("AAPL", bar, book)
    pos = book.get("AAPL")
    assert pos.entry_px == 100.5


def test_stop_order_does_not_fill_when_high_below_trigger():
    fill = SimulatedFillEngine(slippage_bps_by_class={"equity": 0.0, "crypto": 0.0})
    fill.submit(_order(order_type="stop", limit_price=None, stop_price=101.5))
    book = PositionBook()
    bar = _bar(datetime(2026, 5, 14, 14, 5, tzinfo=timezone.utc), 100.0, 101.0, 99.5, 100.8)
    fills = fill.process_bar("AAPL", bar, book)
    assert fills == []
    assert len(fill.pending) == 1


def test_initial_stop_px_set_on_filled_position():
    fill = SimulatedFillEngine(slippage_bps_by_class={"equity": 0.0, "crypto": 0.0})
    fill.submit(_order(limit_price=100.0, stop_price=99.0))
    book = PositionBook()
    bar = _bar(datetime(2026, 5, 14, 14, 5, tzinfo=timezone.utc), 100.5, 101.0, 99.5, 100.8)
    fill.process_bar("AAPL", bar, book)
    pos = book.get("AAPL")
    assert pos.initial_stop_px == 99.0
    assert pos.initial_risk_per_share == 1.0


def test_unrelated_symbol_left_pending():
    fill = SimulatedFillEngine(slippage_bps_by_class={"equity": 0.0, "crypto": 0.0})
    fill.submit(_order(symbol="MSFT", limit_price=100.0))
    fill.submit(_order(symbol="AAPL", limit_price=100.0))
    book = PositionBook()
    bar = _bar(datetime(2026, 5, 14, 14, 5, tzinfo=timezone.utc), 100.5, 101.0, 99.5, 100.8)
    fill.process_bar("AAPL", bar, book)
    # MSFT order was not processed and stays in pending
    assert any(p.symbol == "MSFT" for p in fill.pending)
    assert all(p.symbol != "AAPL" for p in fill.pending)
