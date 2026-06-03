"""Exit-side fill gate — symmetric counterpart to test_position_manager_fill_gate.

Once OrderExecutor has flipped exit_submitted on a position, PositionManager
must stop emitting actions for that position on subsequent bars, AND must
not bump bars_held (so a stale time_stop can't re-fire if the flag flips
back somehow).
"""
from __future__ import annotations
from datetime import datetime, timezone

from core.bar import Bar
from core.position_manager import PositionManager
from state.position_book import OpenPosition, PositionBook


def _make_pos(**overrides) -> OpenPosition:
    base = dict(
        symbol="COIN", setup="price_discovery", side="short",
        qty=1.0, entry_px=174.07, stop_px=175.31, target_px=171.60,
        opened_at=datetime(2026, 6, 2, 15, 0, tzinfo=timezone.utc),
        order_id="abc",
        fill_confirmed=True,  # past the fill gate
        bars_held=0,
    )
    base.update(overrides)
    return OpenPosition(**base)


def _make_bar(close: float = 175.50) -> Bar:
    return Bar(
        symbol="COIN",
        ts=datetime(2026, 6, 2, 16, 5, tzinfo=timezone.utc),
        open=close, high=close, low=close, close=close, volume=1000,
    )


def test_on_bar_skips_position_with_exit_submitted_true():
    book = PositionBook()
    pos = _make_pos(exit_submitted=True)
    book.add(pos)
    pm = PositionManager(book=book, max_hold_bars=12, breakeven_at_R=1.0)

    actions = pm.on_bar("COIN", _make_bar(close=175.50))  # would normally hit stop

    assert actions == []


def test_on_bar_does_not_bump_bars_held_when_exit_submitted():
    book = PositionBook()
    pos = _make_pos(exit_submitted=True, bars_held=3)
    book.add(pos)
    pm = PositionManager(book=book, max_hold_bars=12, breakeven_at_R=1.0)

    pm.on_bar("COIN", _make_bar())

    assert book.get("COIN", "price_discovery").bars_held == 3


def test_on_bar_emits_when_exit_submitted_false():
    book = PositionBook()
    pos = _make_pos(exit_submitted=False)
    book.add(pos)
    pm = PositionManager(book=book, max_hold_bars=12, breakeven_at_R=1.0)

    actions = pm.on_bar("COIN", _make_bar(close=175.50))  # short, high>=stop

    assert len(actions) == 1
    assert actions[0].kind == "stop"


def test_on_bar_exit_gate_runs_after_fill_gate():
    """A position that's both unconfirmed-fill AND exit-submitted should
    skip at the fill gate and never reach the exit gate (well-defined
    behavior — gates compose; either gate skipping the position is fine)."""
    book = PositionBook()
    pos = _make_pos(fill_confirmed=False, exit_submitted=True)
    book.add(pos)
    pm = PositionManager(
        book=book, max_hold_bars=12, breakeven_at_R=1.0,
        order_status_for=lambda p: None,  # fill gate stays closed
    )

    actions = pm.on_bar("COIN", _make_bar(close=175.50))

    assert actions == []
    assert book.get("COIN", "price_discovery").bars_held == 0
