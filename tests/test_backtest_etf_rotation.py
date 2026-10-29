"""Unit tests for Dual-Momentum ETF Rotation Backtest."""

import pytest
import pandas as pd
from scripts.backtest_etf_rotation import (
    generate_synthetic_data,
    run_simulation,
    compute_metrics,
)
from strategies.dual_momentum_etf import DualMomentumConfig


def test_synthetic_backtest_run():
    satellites = ["SMGB", "QDVE", "EQQQ", "ISPY", "XUFN", "SXLE", "XUIN"]
    safe_harbor = "CSH2"
    benchmark = "SPY"

    prices_df, bench_series, vix_series = generate_synthetic_data(
        satellites, benchmark, safe_harbor, start="2020-01-01", end="2023-01-01"
    )

    cfg = DualMomentumConfig(
        satellite_symbols=satellites,
        safe_harbor_symbol=safe_harbor,
        benchmark_symbol=benchmark,
    )

    res = run_simulation(
        prices_df=prices_df,
        benchmark_series=bench_series,
        vix_series=vix_series,
        cfg=cfg,
        initial_capital=100_000.0,
    )

    assert "equity_curve" in res
    assert "rebalance_log" in res
    assert not res["equity_curve"].empty

    metrics = compute_metrics(res["equity_curve"], 100_000.0)
    assert "cagr_pct" in metrics
    assert "sharpe" in metrics
    assert "max_dd_pct" in metrics
    assert metrics["final_equity"] > 0
