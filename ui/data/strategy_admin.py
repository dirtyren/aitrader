"""Read/write helpers for the dashboard's strategy admin table."""
from __future__ import annotations

from datetime import datetime

import pandas as pd

from state.mysql_store import MySQLStore
from ui.data import stats
from ui.data.trades_repo import get_closed_trades


_BASE_COLS = [
    "id", "name", "state", "enabled", "open_count",
    "today_pnl", "total_pnl", "win_rate", "trade_count",
    "last_change_at", "last_change_reason",
]
_PERIOD_COLS = [
    "period_pnl", "period_win_rate", "period_sharpe",
    "period_max_dd", "period_avg_r", "period_trade_count",
]
_ADMIN_COLS = _BASE_COLS + _PERIOD_COLS


def get_admin_view(
    store: MySQLStore, start: datetime, end: datetime,
) -> pd.DataFrame:
    """Per-strategy admin rows joined with period-scoped KPIs.

    SQL aggregator (`store.get_strategies_admin_view`) gives state, open
    count, today P&L, all-time P&L, all-time win rate. We add period-scoped
    Sharpe / Max DD / Avg R / period P&L / period win rate by running
    `stats.compute_kpis` on the closed-trades dataframe for [start, end).
    """
    rows = store.get_strategies_admin_view()
    if not rows:
        return pd.DataFrame(columns=_ADMIN_COLS)

    enriched: list[dict] = []
    for row in rows:
        df = get_closed_trades(row["name"], start, end)
        kpis = stats.compute_kpis(df)
        enriched.append({
            **row,
            "period_pnl": float(kpis.total_pnl),
            "period_win_rate": kpis.win_rate,
            "period_sharpe": kpis.sharpe,
            "period_max_dd": float(kpis.max_drawdown),
            "period_avg_r": kpis.expectancy_R,
            "period_trade_count": int(kpis.trade_count),
        })
    return pd.DataFrame(enriched, columns=_ADMIN_COLS)
