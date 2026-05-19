"""Performance metrics for the per-trade R-multiple model.

compute_metrics is the single entry point. It consumes the equity curve
(pd.Series indexed by bar timestamps) and trade ledger (pd.DataFrame with
at least pnl_usd + R_realized columns) produced by IntradayReplay.
"""
from __future__ import annotations
import math

import pandas as pd


_BARS_PER_TRADING_DAY = 78    # 6.5 h * 12 five-minute bars
_TRADING_DAYS = 252


def compute_metrics(equity_curve: pd.Series, trades: pd.DataFrame) -> dict:
    if equity_curve.empty:
        return _empty_metrics()
    max_dd = float(_max_drawdown(equity_curve))
    if trades.empty:
        return {**_empty_metrics(), "max_drawdown": max_dd}

    pnl = trades["pnl_usd"]
    wins_total = pnl[pnl > 0].sum()
    losses_total = abs(pnl[pnl < 0].sum())
    if losses_total > 0:
        profit_factor = float(wins_total / losses_total)
    elif wins_total > 0:
        profit_factor = float("inf")
    else:
        profit_factor = 0.0

    rets = equity_curve.pct_change().dropna()
    if rets.std() > 0:
        sharpe = float(rets.mean() / rets.std() * math.sqrt(_BARS_PER_TRADING_DAY * _TRADING_DAYS))
    else:
        sharpe = 0.0

    return {
        "trades": int(len(trades)),
        "win_rate": float((pnl > 0).mean()),
        "max_drawdown": max_dd,
        "avg_R": float(trades["R_realized"].mean()),
        "profit_factor": profit_factor,
        "sharpe": sharpe,
    }


def _empty_metrics() -> dict:
    return {
        "trades": 0,
        "win_rate": 0.0,
        "max_drawdown": 0.0,
        "avg_R": 0.0,
        "profit_factor": 0.0,
        "sharpe": 0.0,
    }


def _max_drawdown(equity_curve: pd.Series) -> float:
    if equity_curve.empty:
        return 0.0
    peak = equity_curve.cummax()
    dd = (peak - equity_curve) / peak
    return float(dd.max() if not dd.empty else 0.0)
