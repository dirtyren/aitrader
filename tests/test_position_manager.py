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


def _adopted_pos(side="long", entry=100.0, stop=99.0, target=102.0):
    return OpenPosition(symbol="AAPL", setup="adopted", side=side,
                        qty=10, entry_px=entry, stop_px=stop, target_px=target,
                        opened_at=datetime(2026, 5, 14, 14, 0, tzinfo=timezone.utc),
                        order_id="", adopted=True)


def test_adopted_position_triggers_stop_action():
    """Adopted positions DO check stop-loss levels (fix 2026-05-26)."""
    book = PositionBook()
    book.add(_adopted_pos())  # long, stop 99, entry 100
    pm = PositionManager(book, max_hold_bars=12, breakeven_at_R=1.0)
    actions = pm.on_bar("AAPL",
        _bar(datetime(2026, 5, 14, 14, 5, tzinfo=timezone.utc), 98.5, l=98.0))
    assert any(a.kind == "stop" for a in actions)
    assert book.get("AAPL") is None  # position closed


def test_adopted_position_triggers_target_action():
    """Adopted positions DO check take-profit levels (fix 2026-05-26)."""
    book = PositionBook()
    book.add(_adopted_pos())
    pm = PositionManager(book, max_hold_bars=12, breakeven_at_R=1.0)
    actions = pm.on_bar("AAPL",
        _bar(datetime(2026, 5, 14, 14, 5, tzinfo=timezone.utc), 102.5, h=103.0))
    assert any(a.kind == "target" for a in actions)
    assert book.get("AAPL") is None  # position closed


def test_adopted_position_skips_breakeven():
    book = PositionBook()
    book.add(_adopted_pos())
    pm = PositionManager(book, max_hold_bars=12, breakeven_at_R=1.0)
    pm.on_bar("AAPL",
        _bar(datetime(2026, 5, 14, 14, 5, tzinfo=timezone.utc), 101.2, h=101.5))
    pos = book.get("AAPL")
    assert pos.stop_px == 99.0
    assert pos.breakeven_moved is False


def test_adopted_position_skips_time_stop():
    book = PositionBook()
    book.add(_adopted_pos())
    pm = PositionManager(book, max_hold_bars=2, breakeven_at_R=1.0)
    base = datetime(2026, 5, 14, 14, 0, tzinfo=timezone.utc)
    pm.on_bar("AAPL", _bar(base + timedelta(minutes=5), 100.5))
    pm.on_bar("AAPL", _bar(base + timedelta(minutes=10), 100.5))
    actions = pm.on_bar("AAPL", _bar(base + timedelta(minutes=15), 100.5))
    assert actions == []
    assert book.get("AAPL") is not None


def test_adopted_position_increments_bars_held():
    book = PositionBook()
    book.add(_adopted_pos())
    pm = PositionManager(book, max_hold_bars=12, breakeven_at_R=1.0)
    base = datetime(2026, 5, 14, 14, 0, tzinfo=timezone.utc)
    pm.on_bar("AAPL", _bar(base + timedelta(minutes=5), 100.5))
    pm.on_bar("AAPL", _bar(base + timedelta(minutes=10), 100.5))
    assert book.get("AAPL").bars_held == 2


def test_adopted_position_with_none_stop_does_not_raise():
    book = PositionBook()
    p = OpenPosition(symbol="BTC/USD", setup="adopted", side="long",
                     qty=1, entry_px=50_000.0, stop_px=None, target_px=None,
                     opened_at=datetime(2026, 5, 14, 14, 0, tzinfo=timezone.utc),
                     order_id="", adopted=True)
    book.add(p)
    pm = PositionManager(book, max_hold_bars=12, breakeven_at_R=1.0)
    actions = pm.on_bar("BTC/USD",
        Bar(symbol="BTC/USD",
            ts=datetime(2026, 5, 14, 14, 5, tzinfo=timezone.utc),
            open=49_000.0, high=49_500.0, low=48_000.0, close=48_500.0,
            volume=10))
    assert actions == []
