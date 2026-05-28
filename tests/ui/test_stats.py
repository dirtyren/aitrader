import pandas as pd
import pytest

from ui.data.stats import compute_kpis, KPIs


def _empty_trades_df() -> pd.DataFrame:
    return pd.DataFrame(columns=[
        "opened_at", "closed_at", "symbol", "setup_name", "side", "qty",
        "entry_px", "exit_px", "stop_px", "target_px", "initial_stop_px",
        "pnl_usd", "R_realized", "close_reason", "bars_held",
    ])


def test_compute_kpis_empty_df_returns_zeros():
    kpis = compute_kpis(_empty_trades_df())
    assert kpis.total_pnl == 0.0
    assert kpis.trade_count == 0
    assert kpis.win_rate is None
    assert kpis.avg_win is None
    assert kpis.avg_loss is None
    assert kpis.profit_factor is None
    assert kpis.expectancy_R is None
    assert kpis.max_drawdown == 0.0
    assert kpis.sharpe is None
    assert kpis.avg_bars_held is None
    assert kpis.best_trade is None
    assert kpis.worst_trade is None
