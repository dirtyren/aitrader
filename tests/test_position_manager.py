from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock
from core.bar import Bar
from core.position_manager import PositionManager, PositionAction
from state.position_book import PositionBook, OpenPosition


def _bar(ts, c, h=None, l=None):
    return Bar(symbol="AAPL", ts=ts, open=c, high=h or c + 0.1,
               low=l or c - 0.1, close=c, volume=100)


def _open_pos(side="long", entry=100, stop=99, target=102):
    return OpenPosition(symbol="AAPL", setup="price_discovery", side=side,
                        qty=10, entry_px=entry, stop_px=stop, target_px=target,
                        opened_at=datetime(2026, 5, 14, 14, 0, tzinfo=timezone.utc),
                        order_id="x")


def test_stop_hit_long():
    book = PositionBook()
    book.add(_open_pos())
    pm = PositionManager(book, max_hold_bars=12, breakeven_at_R=1.0)
    actions = pm.on_bar("AAPL", _bar(datetime(2026, 5, 14, 14, 5, tzinfo=timezone.utc), 98.5, l=98.0))
    assert any(a.kind == "stop" for a in actions)


def test_target_hit_long():
    book = PositionBook()
    book.add(_open_pos())
    pm = PositionManager(book, max_hold_bars=12, breakeven_at_R=1.0)
    actions = pm.on_bar("AAPL", _bar(datetime(2026, 5, 14, 14, 5, tzinfo=timezone.utc), 102.5, h=103.0))
    assert any(a.kind == "target" for a in actions)


def test_breakeven_moves_stop_to_entry():
    book = PositionBook()
    book.add(_open_pos())             # risk = 1, target = 102 (R=2)
    pm = PositionManager(book, max_hold_bars=12, breakeven_at_R=1.0)
    pm.on_bar("AAPL", _bar(datetime(2026, 5, 14, 14, 5, tzinfo=timezone.utc), 101.2, h=101.5))
    assert book.get("AAPL").stop_px == 100.0
    assert book.get("AAPL").breakeven_moved is True


def test_time_stop_triggers_after_max_hold():
    book = PositionBook()
    book.add(_open_pos())
    pm = PositionManager(book, max_hold_bars=2, breakeven_at_R=1.0)
    base = datetime(2026, 5, 14, 14, 0, tzinfo=timezone.utc)
    pm.on_bar("AAPL", _bar(base + timedelta(minutes=5), 100.5))
    pm.on_bar("AAPL", _bar(base + timedelta(minutes=10), 100.6))
    actions = pm.on_bar("AAPL", _bar(base + timedelta(minutes=15), 100.7))
    assert any(a.kind == "time_stop" for a in actions)
