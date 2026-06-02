"""record_exits_to_ledger defers MySQL closes to the broker fill.

Before the fix, this function called `mysql_store.position_closed` the
moment PositionManager.on_bar emitted a stop/target/time_stop action.
That double-booked closes (engine writes at stop_px, reconciler writes
the actual fill price later) AND wrote phantom -PnL trades for entries
the broker never filled.

After the fix:
  - DailyLedger gets a TradeRecord (so live PnL views update intraday).
  - mysql_store.position_closed is NEVER called from this function.
  - apply_tagged_fill (reconciler/fills.py) is the only path that writes
    MySQL closes for stop/target/time_stop, and it uses the broker's
    actual fill price + role-derived close_reason.
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from core.bar import Bar
from core.position_manager import PositionAction
from scheduler.loop import record_exits_to_ledger
from state.daily_ledger import DailyLedger
from state.position_book import OpenPosition


def _ts():
    return datetime(2026, 6, 1, 14, 5, tzinfo=timezone.utc)


def _bar(symbol="UBER", c=72.5):
    return Bar(symbol=symbol, ts=_ts(), open=c, high=max(c + 0.1, c),
               low=min(c - 0.1, c), close=c, volume=100)


def _pos(*, symbol="UBER", setup="rsi_reversion", side="long",
         entry=73.87, stop=72.39):
    return OpenPosition(
        symbol=symbol, setup=setup, side=side, qty=2,
        entry_px=entry, stop_px=stop, target_px=76.09,
        opened_at=_ts(), order_id="alp-1", initial_stop_px=stop,
        fill_confirmed=True,
    )


@pytest.mark.parametrize("kind,asset_class", [
    ("stop", "equity"),
    ("target", "equity"),
    ("time_stop", "equity"),
    ("stop", "crypto"),
    ("target", "crypto"),
    ("time_stop", "crypto"),
])
def test_exits_skip_mysql_position_closed(kind, asset_class):
    """No matter the asset class or exit kind, the engine no longer writes
    to MySQL — the broker fill (handled by reconciler/fills.py) is the
    only authoritative close path."""
    ledger = DailyLedger(initial_equity=100_000)
    pos = _pos()
    action = PositionAction(
        symbol="UBER", setup="rsi_reversion", kind=kind, price=72.39,
        qty=2.0, side="long",
    )
    mysql = MagicMock()

    records = record_exits_to_ledger(
        ledger, "UBER", [action], _bar(),
        mysql_store=mysql,
        positions_snapshot={"rsi_reversion": pos},
        asset_class=asset_class,
    )

    assert len(records) == 1
    assert records[0].setup == "rsi_reversion"
    mysql.position_closed.assert_not_called()


def test_breakeven_action_is_not_recorded_or_written():
    """Breakeven adjusts the stop level — it's not an exit. Pre-fix it was
    silently ignored here too; verify that's still the case."""
    ledger = DailyLedger(initial_equity=100_000)
    pos = _pos()
    action = PositionAction(
        symbol="UBER", setup="rsi_reversion", kind="breakeven", price=73.87,
        qty=2.0, side="long",
    )
    mysql = MagicMock()

    records = record_exits_to_ledger(
        ledger, "UBER", [action], _bar(),
        mysql_store=mysql,
        positions_snapshot={"rsi_reversion": pos},
        asset_class="equity",
    )

    assert records == []
    mysql.position_closed.assert_not_called()


def test_no_snapshot_logs_warning_and_skips():
    """If the snapshot is missing the setup, we can't compute PnL — skip."""
    ledger = DailyLedger(initial_equity=100_000)
    action = PositionAction(
        symbol="UBER", setup="rsi_reversion", kind="stop", price=72.39,
        qty=2.0, side="long",
    )
    mysql = MagicMock()

    records = record_exits_to_ledger(
        ledger, "UBER", [action], _bar(),
        mysql_store=mysql,
        positions_snapshot={},
        asset_class="equity",
    )

    assert records == []
    mysql.position_closed.assert_not_called()
