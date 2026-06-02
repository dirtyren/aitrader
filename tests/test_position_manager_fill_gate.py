"""PositionManager fill-confirmation gate.

`OpenPosition.fill_confirmed` defaults to False. on_bar must NOT emit any
exit action for a position whose fill hasn't been confirmed at the broker
— that's how a phantom -PnL row got written for UBER (entry never
filled, but a low bar's price grazed the bracket stop level).

The gate polls Alpaca once via the injected `order_status_for` callable.
Status in {filled, partially_filled} flips the flag and unlocks the
position. Any other status (or a None / exception) leaves the gate
closed and the position untouched until next bar.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from core.bar import Bar
from core.position_manager import PositionManager
from state.position_book import OpenPosition, PositionBook


def _bar(ts, c=100.0, h=None, l=None):
    return Bar(symbol="UBER", ts=ts, open=c, high=h or c + 0.1,
               low=l or c - 0.1, close=c, volume=100)


def _open_pos(*, fill_confirmed=False):
    return OpenPosition(
        symbol="UBER", setup="rsi_reversion", side="long",
        qty=2, entry_px=73.87, stop_px=72.39, target_px=76.09,
        opened_at=datetime(2026, 6, 1, 14, 0, tzinfo=timezone.utc),
        order_id="alp-uber-1", initial_stop_px=72.39,
        fill_confirmed=fill_confirmed,
    )


def test_unconfirmed_position_blocks_stop_action_when_status_accepted():
    """The bug we're fixing: bar.low touches stop_px BUT the broker has
    only `accepted` the limit (filled_qty=0). No action must be emitted."""
    book = PositionBook()
    book.add(_open_pos(fill_confirmed=False))
    statuses = []

    def order_status_for(pos):
        statuses.append(pos.order_id)
        return "accepted"

    pm = PositionManager(
        book, max_hold_bars=12, breakeven_at_R=1.0,
        order_status_for=order_status_for,
    )
    actions = pm.on_bar(
        "UBER", _bar(datetime(2026, 6, 1, 14, 5, tzinfo=timezone.utc),
                     c=72.5, l=72.0),
    )

    assert actions == []
    assert statuses == ["alp-uber-1"]
    pos = book.get("UBER")
    assert pos is not None  # not closed
    assert pos.fill_confirmed is False  # still pending
    assert pos.bars_held == 0  # gate skipped bars_held increment too


def test_filled_status_flips_flag_and_emits_action():
    book = PositionBook()
    book.add(_open_pos(fill_confirmed=False))
    flipped = []

    pm = PositionManager(
        book, max_hold_bars=12, breakeven_at_R=1.0,
        order_status_for=lambda pos: "filled",
        on_fill_confirmed=lambda pos: flipped.append(pos.symbol),
    )
    actions = pm.on_bar(
        "UBER", _bar(datetime(2026, 6, 1, 14, 5, tzinfo=timezone.utc),
                     c=72.5, l=72.0),
    )

    assert any(a.kind == "stop" for a in actions)
    assert flipped == ["UBER"]
    # In-memory book entry is closed by the exit path (existing semantics).


def test_partially_filled_status_flips_flag():
    book = PositionBook()
    book.add(_open_pos(fill_confirmed=False))
    pm = PositionManager(
        book, max_hold_bars=12, breakeven_at_R=1.0,
        order_status_for=lambda pos: "partially_filled",
    )
    actions = pm.on_bar(
        "UBER", _bar(datetime(2026, 6, 1, 14, 5, tzinfo=timezone.utc),
                     c=72.5, l=72.0),
    )
    assert any(a.kind == "stop" for a in actions)


@pytest.mark.parametrize(
    "status",
    ["new", "accepted", "held", "pending_new", "accepted_for_bidding",
     "canceled", "expired", "rejected", None],
)
def test_non_filled_status_blocks(status):
    book = PositionBook()
    book.add(_open_pos(fill_confirmed=False))
    pm = PositionManager(
        book, max_hold_bars=12, breakeven_at_R=1.0,
        order_status_for=lambda pos: status,
    )
    actions = pm.on_bar(
        "UBER", _bar(datetime(2026, 6, 1, 14, 5, tzinfo=timezone.utc),
                     c=72.5, l=72.0),
    )
    assert actions == []
    pos = book.get("UBER")
    assert pos.fill_confirmed is False


def test_lookup_exception_does_not_crash_and_blocks():
    book = PositionBook()
    book.add(_open_pos(fill_confirmed=False))

    def boom(pos):
        raise RuntimeError("Alpaca timeout")

    pm = PositionManager(
        book, max_hold_bars=12, breakeven_at_R=1.0,
        order_status_for=boom,
    )
    actions = pm.on_bar(
        "UBER", _bar(datetime(2026, 6, 1, 14, 5, tzinfo=timezone.utc),
                     c=72.5, l=72.0),
    )
    assert actions == []
    assert book.get("UBER") is not None


def test_already_confirmed_position_does_not_poll():
    """fill_confirmed=True short-circuits the gate — no Alpaca poll, no
    extra cost on every bar."""
    book = PositionBook()
    book.add(_open_pos(fill_confirmed=True))
    polled = []

    pm = PositionManager(
        book, max_hold_bars=12, breakeven_at_R=1.0,
        order_status_for=lambda pos: polled.append(pos.order_id) or "filled",
    )
    actions = pm.on_bar(
        "UBER", _bar(datetime(2026, 6, 1, 14, 5, tzinfo=timezone.utc),
                     c=72.5, l=72.0),
    )

    assert any(a.kind == "stop" for a in actions)
    assert polled == []  # no broker call


def test_default_order_status_for_returns_none_blocks():
    """No callable injected (e.g. unit tests, dry-run modes): the gate
    treats every unconfirmed position as 'still pending'."""
    book = PositionBook()
    book.add(_open_pos(fill_confirmed=False))
    pm = PositionManager(book, max_hold_bars=12, breakeven_at_R=1.0)
    actions = pm.on_bar(
        "UBER", _bar(datetime(2026, 6, 1, 14, 5, tzinfo=timezone.utc),
                     c=72.5, l=72.0),
    )
    assert actions == []
