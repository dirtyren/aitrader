"""Read-only view of open positions across all strategies."""
from __future__ import annotations

import pandas as pd
from sqlalchemy import text

from ui.data.db import get_engine


_OPEN_COLS = [
    "strategy", "symbol", "asset_class", "setup_name", "side", "qty",
    "entry_px", "stop_px", "target_px", "initial_stop_px",
    "opened_at", "status",
]


def get_open() -> pd.DataFrame:
    """All open positions across strategies, joined to the strategy name."""
    eng = get_engine()
    with eng.connect() as conn:
        df = pd.read_sql(
            text("""
                SELECT s.name AS strategy,
                       p.symbol, p.asset_class, p.setup_name, p.side, p.qty,
                       p.entry_px, p.stop_px, p.target_px, p.initial_stop_px,
                       p.opened_at, p.status
                FROM positions p
                JOIN strategies s ON s.id = p.strategy_id
                WHERE p.status = 'open'
                ORDER BY s.name, p.opened_at
            """),
            conn,
        )
    if df.empty:
        return pd.DataFrame(columns=_OPEN_COLS)
    return df
