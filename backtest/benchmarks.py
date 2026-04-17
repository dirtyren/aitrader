"""
Benchmark strategies for comparison against the regime-based system.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from backtest.performance import PerformanceEngine


def buy_and_hold(price_series: pd.Series) -> dict:
    """Passive buy-and-hold benchmark.

    Parameters
    ----------
    price_series:
        Daily close prices.

    Returns
    -------
    dict with keys: total_return, sharpe_ratio, max_drawdown, calmar_ratio,
    daily_returns
    """
    engine = PerformanceEngine(slippage_bps=5.0, commission_bps=2.0)
    # Always fully invested (allocation = 1.0)
    signals = pd.Series(1.0, index=price_series.index)
    return engine.run(price_series, signals)


def sma_200_strategy(price_series: pd.Series) -> dict:
    """200-day SMA trend-following benchmark.

    Invest fully (allocation=1.0) when price is above the 200-day SMA,
    otherwise stay flat (allocation=0.0).  Uses the same PerformanceEngine
    slippage/commission as the live system.

    Parameters
    ----------
    price_series:
        Daily close prices.

    Returns
    -------
    dict with keys: total_return, sharpe_ratio, max_drawdown, calmar_ratio,
    daily_returns
    """
    sma200 = price_series.rolling(window=200, min_periods=200).mean()
    allocation = (price_series > sma200).astype(float)
    # Before we have 200 bars of history keep allocation at 0
    allocation = allocation.where(sma200.notna(), other=0.0)

    engine = PerformanceEngine(slippage_bps=5.0, commission_bps=2.0)
    return engine.run(price_series, allocation)


def random_entry_control(
    price_series: pd.Series,
    risk_manager_fn=None,
    n_simulations: int = 100,
    seed: int = 42,
) -> dict:
    """Control group: random binary (0/1) entry signals.

    Runs n_simulations independent random-entry simulations and returns the
    mean and standard deviation of each metric across runs.

    Parameters
    ----------
    price_series:
        Daily close prices.
    risk_manager_fn:
        Optional callable ``(allocation: float) -> float`` applied to each
        allocation value before passing to the engine (e.g. a risk cap).
    n_simulations:
        Number of Monte-Carlo simulation runs (default 100).
    seed:
        Base random seed for reproducibility (default 42).

    Returns
    -------
    dict with keys:
        mean_total_return, std_total_return,
        mean_sharpe,        std_sharpe,
        mean_max_drawdown,  std_max_drawdown,
        mean_calmar_ratio,  std_calmar_ratio
    """
    engine = PerformanceEngine(slippage_bps=5.0, commission_bps=2.0)
    rng = np.random.default_rng(seed)

    total_returns: list[float] = []
    sharpes: list[float] = []
    max_drawdowns: list[float] = []
    calmars: list[float] = []

    for i in range(n_simulations):
        # Draw a new seed per simulation for independence
        sim_seed = int(rng.integers(0, 2**31))
        sim_rng = np.random.default_rng(sim_seed)

        raw_alloc = sim_rng.integers(0, 2, size=len(price_series)).astype(float)
        allocation = pd.Series(raw_alloc, index=price_series.index)

        if risk_manager_fn is not None:
            allocation = allocation.apply(risk_manager_fn)

        result = engine.run(price_series, allocation)
        total_returns.append(result["total_return"])
        sharpes.append(result["sharpe_ratio"] if not np.isnan(result["sharpe_ratio"]) else 0.0)
        max_drawdowns.append(result["max_drawdown"])
        calmars.append(result["calmar_ratio"] if not np.isnan(result["calmar_ratio"]) else 0.0)

    return {
        "mean_total_return": float(np.mean(total_returns)),
        "std_total_return": float(np.std(total_returns)),
        "mean_sharpe": float(np.mean(sharpes)),
        "std_sharpe": float(np.std(sharpes)),
        "mean_max_drawdown": float(np.mean(max_drawdowns)),
        "std_max_drawdown": float(np.std(max_drawdowns)),
        "mean_calmar_ratio": float(np.mean(calmars)),
        "std_calmar_ratio": float(np.std(calmars)),
    }
