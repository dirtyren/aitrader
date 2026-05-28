from datetime import datetime, timezone, timedelta

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


def _trades(rows: list[dict]) -> pd.DataFrame:
    """Build a trades DataFrame with sensible defaults; rows override."""
    base = {
        "opened_at": datetime(2026, 5, 1, 14, 0, tzinfo=timezone.utc),
        "closed_at": datetime(2026, 5, 1, 15, 0, tzinfo=timezone.utc),
        "symbol": "AAPL", "setup_name": "vwap_bounce", "side": "long",
        "qty": 1.0, "entry_px": 100.0, "exit_px": 101.0,
        "stop_px": 99.0, "target_px": 102.0, "initial_stop_px": 99.0,
        "pnl_usd": 1.0, "R_realized": 1.0, "close_reason": "target", "bars_held": 5,
    }
    return pd.DataFrame([{**base, **r} for r in rows])


def test_compute_kpis_all_wins():
    df = _trades([
        {"pnl_usd": 100.0, "R_realized": 1.0},
        {"pnl_usd": 200.0, "R_realized": 2.0,
         "closed_at": datetime(2026, 5, 2, 15, 0, tzinfo=timezone.utc)},
    ])
    k = compute_kpis(df)
    assert k.total_pnl == 300.0
    assert k.trade_count == 2
    assert k.win_rate == 1.0
    assert k.avg_win == 150.0
    assert k.avg_loss is None
    assert k.profit_factor is None  # no losses → undefined
    assert k.expectancy_R == 1.5
    assert k.max_drawdown == 0.0
    assert k.best_trade == 200.0
    assert k.worst_trade == 100.0


def test_compute_kpis_all_losses():
    df = _trades([
        {"pnl_usd": -50.0, "R_realized": -1.0},
        {"pnl_usd": -30.0, "R_realized": -0.6,
         "closed_at": datetime(2026, 5, 2, 15, 0, tzinfo=timezone.utc)},
    ])
    k = compute_kpis(df)
    assert k.total_pnl == -80.0
    assert k.win_rate == 0.0
    assert k.avg_win is None
    assert k.avg_loss == -40.0
    assert k.profit_factor == 0.0  # gross_win 0 / gross_loss 80
    assert k.max_drawdown == -80.0


def test_compute_kpis_mixed_with_drawdown():
    """Sequence: +100, -150, +50, +200 → cum 100, -50, 0, 200; drawdown peak 100→-50 = -150."""
    df = _trades([
        {"pnl_usd": 100.0, "R_realized": 1.0,
         "closed_at": datetime(2026, 5, 1, 15, 0, tzinfo=timezone.utc)},
        {"pnl_usd": -150.0, "R_realized": -1.5,
         "closed_at": datetime(2026, 5, 2, 15, 0, tzinfo=timezone.utc)},
        {"pnl_usd": 50.0, "R_realized": 0.5,
         "closed_at": datetime(2026, 5, 3, 15, 0, tzinfo=timezone.utc)},
        {"pnl_usd": 200.0, "R_realized": 2.0,
         "closed_at": datetime(2026, 5, 4, 15, 0, tzinfo=timezone.utc)},
    ])
    k = compute_kpis(df)
    assert k.total_pnl == 200.0
    assert k.trade_count == 4
    assert k.win_rate == 0.75
    assert k.avg_win == pytest.approx((100 + 50 + 200) / 3)
    assert k.avg_loss == -150.0
    assert k.profit_factor == pytest.approx(350.0 / 150.0)
    assert k.expectancy_R == pytest.approx((1.0 - 1.5 + 0.5 + 2.0) / 4)
    assert k.max_drawdown == pytest.approx(-150.0)
    assert k.best_trade == 200.0
    assert k.worst_trade == -150.0


def test_compute_kpis_single_trade_returns_none_sharpe():
    df = _trades([{"pnl_usd": 100.0}])
    k = compute_kpis(df)
    assert k.sharpe is None  # need ≥2 daily samples


from ui.data.stats import (
    equity_curve, daily_pnl, r_distribution, winloss_by_setup,
)


def test_equity_curve_cumulative_sorted_by_close_time():
    df = _trades([
        {"pnl_usd": 50.0,
         "closed_at": datetime(2026, 5, 2, 15, 0, tzinfo=timezone.utc)},
        {"pnl_usd": 100.0,
         "closed_at": datetime(2026, 5, 1, 15, 0, tzinfo=timezone.utc)},
        {"pnl_usd": -30.0,
         "closed_at": datetime(2026, 5, 3, 15, 0, tzinfo=timezone.utc)},
    ])
    eq = equity_curve(df)
    assert list(eq.columns) == ["closed_at", "cum_pnl"]
    assert list(eq["cum_pnl"]) == [100.0, 150.0, 120.0]


def test_daily_pnl_groups_by_calendar_day():
    df = _trades([
        {"pnl_usd": 50.0,
         "closed_at": datetime(2026, 5, 1, 14, 0, tzinfo=timezone.utc)},
        {"pnl_usd": 30.0,
         "closed_at": datetime(2026, 5, 1, 18, 0, tzinfo=timezone.utc)},
        {"pnl_usd": -20.0,
         "closed_at": datetime(2026, 5, 2, 10, 0, tzinfo=timezone.utc)},
    ])
    dp = daily_pnl(df)
    assert list(dp.columns) == ["day", "pnl"]
    assert len(dp) == 2
    assert dp.iloc[0]["pnl"] == 80.0
    assert dp.iloc[1]["pnl"] == -20.0


def test_r_distribution_bins():
    df = _trades([
        {"R_realized": 1.0}, {"R_realized": 1.5}, {"R_realized": -0.5},
        {"R_realized": -1.0}, {"R_realized": 2.5},
    ])
    series = r_distribution(df)
    assert series.tolist() == [1.0, 1.5, -0.5, -1.0, 2.5]


def test_winloss_by_setup_counts():
    df = _trades([
        {"setup_name": "vwap_bounce", "pnl_usd": 10.0},
        {"setup_name": "vwap_bounce", "pnl_usd": -5.0},
        {"setup_name": "orb",         "pnl_usd": 20.0},
        {"setup_name": "orb",         "pnl_usd": 15.0},
        {"setup_name": "orb",         "pnl_usd": -8.0},
    ])
    wl = winloss_by_setup(df)
    # columns: setup_name, wins, losses
    assert set(wl.columns) == {"setup_name", "wins", "losses"}
    rows = {r["setup_name"]: (r["wins"], r["losses"]) for _, r in wl.iterrows()}
    assert rows["vwap_bounce"] == (1, 1)
    assert rows["orb"] == (2, 1)
