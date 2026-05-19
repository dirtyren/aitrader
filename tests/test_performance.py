import math

import pandas as pd

from backtest.performance import compute_metrics


def test_compute_metrics_with_trades():
    eq = pd.Series([100_000, 100_050, 99_900, 100_200, 100_150, 100_300])
    trades = pd.DataFrame([
        {"pnl_usd": 50,   "R_realized": 1.0},
        {"pnl_usd": -150, "R_realized": -1.0},
        {"pnl_usd": 300,  "R_realized": 2.0},
        {"pnl_usd": -50,  "R_realized": -0.5},
        {"pnl_usd": 150,  "R_realized": 1.5},
    ])
    m = compute_metrics(eq, trades)
    assert m["trades"] == 5
    assert m["win_rate"] == 3 / 5
    assert m["max_drawdown"] >= 0
    assert m["avg_R"] > 0
    # profit_factor: wins=500, losses=200 → 2.5
    assert abs(m["profit_factor"] - 2.5) < 1e-9


def test_compute_metrics_empty_trades():
    eq = pd.Series([100_000])
    trades = pd.DataFrame(columns=["pnl_usd", "R_realized"])
    m = compute_metrics(eq, trades)
    assert m["trades"] == 0
    assert m["win_rate"] == 0.0
    assert m["max_drawdown"] == 0.0
    assert m["profit_factor"] == 0.0
    assert m["sharpe"] == 0.0


def test_compute_metrics_empty_equity():
    eq = pd.Series([], dtype=float)
    trades = pd.DataFrame(columns=["pnl_usd", "R_realized"])
    m = compute_metrics(eq, trades)
    assert m["trades"] == 0
    assert m["max_drawdown"] == 0.0


def test_max_drawdown_known_curve():
    # Peak 100200 then 99900 → trough 99900: dd = (100050-99900)/100050 ≈ 0.001499
    eq = pd.Series([100_000, 100_050, 99_900, 100_200, 100_150, 100_300])
    m = compute_metrics(eq, pd.DataFrame(columns=["pnl_usd", "R_realized"]))
    assert m["max_drawdown"] > 0
    assert m["max_drawdown"] < 0.01


def test_profit_factor_inf_when_no_losers():
    eq = pd.Series([100_000, 100_500])
    trades = pd.DataFrame([
        {"pnl_usd": 100, "R_realized": 1.0},
        {"pnl_usd": 50,  "R_realized": 0.5},
    ])
    m = compute_metrics(eq, trades)
    assert math.isinf(m["profit_factor"])
