"""Read-only access to the MySQL `trades` table for the dashboard."""
from __future__ import annotations

from datetime import datetime

import pandas as pd
from sqlalchemy import text

from ui.data.db import get_engine

_TRADE_COLS = [
    "id", "strategy_id", "symbol", "asset_class", "setup_name", "side", "qty",
    "entry_px", "exit_px", "stop_px", "target_px", "initial_stop_px",
    "pnl_usd", "R_realized", "close_reason",
    "opened_at", "closed_at", "bars_held",
]


def get_closed_trades(strategy: str, start: datetime, end: datetime) -> pd.DataFrame:
    """Return trades for `strategy` whose closed_at falls in [start, end)."""
    eng = get_engine()
    with eng.connect() as conn:
        sid_row = conn.execute(
            text("SELECT id FROM strategies WHERE name=:n"), {"n": strategy}
        ).one_or_none()
        if sid_row is None:
            return pd.DataFrame(columns=_TRADE_COLS)
        sid = sid_row[0]
        df = pd.read_sql(
            text(f"""
                SELECT {", ".join(_TRADE_COLS)}
                FROM trades
                WHERE strategy_id = :sid
                  AND closed_at >= :start
                  AND closed_at <  :end
                ORDER BY closed_at ASC
            """),
            conn,
            params={"sid": sid, "start": start, "end": end},
        )
    return df


# Strategy names that exist in the strategies table for FK attribution
# (reconciliation_events.strategy_id, operator audit rows, etc.) but are not
# trading strategies. The dashboard hides them from cards / dropdowns.
_SYSTEM_STRATEGIES = frozenset({"reconciler", "operator"})


def list_strategies() -> list[str]:
    """All trading strategy names known to the DB, sorted.

    System strategies (reconciler, operator) registered for FK attribution
    are filtered out — they don't trade and shouldn't render as cards.
    """
    eng = get_engine()
    with eng.connect() as conn:
        rows = conn.execute(text("SELECT name FROM strategies ORDER BY name")).all()
    return [r[0] for r in rows if r[0] not in _SYSTEM_STRATEGIES]
