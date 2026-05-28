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


def list_strategies() -> list[str]:
    """All strategy names known to the DB, sorted."""
    eng = get_engine()
    with eng.connect() as conn:
        rows = conn.execute(text("SELECT name FROM strategies ORDER BY name")).all()
    return [r[0] for r in rows]
