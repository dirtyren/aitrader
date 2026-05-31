"""Read/write helpers for the dashboard's strategy admin table."""
from __future__ import annotations

import pandas as pd

from state.mysql_store import MySQLStore


_ADMIN_COLS = [
    "id", "name", "state", "enabled", "open_count",
    "today_pnl", "total_pnl", "win_rate", "trade_count",
    "last_change_at", "last_change_reason",
]


def get_admin_view(store: MySQLStore) -> pd.DataFrame:
    rows = store.get_strategies_admin_view()
    if not rows:
        return pd.DataFrame(columns=_ADMIN_COLS)
    return pd.DataFrame(rows, columns=_ADMIN_COLS)
