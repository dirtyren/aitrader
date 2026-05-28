"""Pure analytics over a closed-trades DataFrame.

No Streamlit, no DB. Input is a pandas DataFrame matching the schema of
the MySQL `trades` table. Output is a KPIs dataclass plus chart-ready
DataFrames.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd


@dataclass
class KPIs:
    total_pnl: float
    trade_count: int
    win_rate: Optional[float]          # fraction in [0, 1]
    avg_win: Optional[float]
    avg_loss: Optional[float]          # negative
    profit_factor: Optional[float]
    expectancy_R: Optional[float]      # mean R_realized
    max_drawdown: float                # negative or zero, USD
    sharpe: Optional[float]            # daily, annualized * sqrt(252)
    avg_bars_held: Optional[float]
    best_trade: Optional[float]
    worst_trade: Optional[float]


def compute_kpis(df: pd.DataFrame) -> KPIs:
    if df.empty:
        return KPIs(
            total_pnl=0.0, trade_count=0,
            win_rate=None, avg_win=None, avg_loss=None,
            profit_factor=None, expectancy_R=None,
            max_drawdown=0.0, sharpe=None,
            avg_bars_held=None, best_trade=None, worst_trade=None,
        )
    pnl = df["pnl_usd"].astype(float)
    wins = pnl[pnl > 0]
    losses = pnl[pnl < 0]
    gross_win = float(wins.sum())
    gross_loss = float(losses.sum())  # negative
    total_pnl = float(pnl.sum())
    trade_count = int(len(df))
    win_rate = float((pnl > 0).mean())
    avg_win = float(wins.mean()) if len(wins) else None
    avg_loss = float(losses.mean()) if len(losses) else None
    profit_factor = (gross_win / abs(gross_loss)) if gross_loss < 0 else None
    expectancy_R = float(df["R_realized"].astype(float).mean())
    max_drawdown = _max_drawdown(df)
    sharpe = _sharpe(df)
    avg_bars_held = float(df["bars_held"].astype(float).mean())
    best_trade = float(pnl.max())
    worst_trade = float(pnl.min())
    return KPIs(
        total_pnl=total_pnl, trade_count=trade_count, win_rate=win_rate,
        avg_win=avg_win, avg_loss=avg_loss, profit_factor=profit_factor,
        expectancy_R=expectancy_R, max_drawdown=max_drawdown,
        sharpe=sharpe, avg_bars_held=avg_bars_held,
        best_trade=best_trade, worst_trade=worst_trade,
    )


def _max_drawdown(df: pd.DataFrame) -> float:
    """Peak-to-trough drawdown of cumulative PnL, ordered by closed_at.
    Anchored at 0 so a series of pure losses produces the cumulative loss as drawdown.
    """
    s = df.sort_values("closed_at")["pnl_usd"].astype(float).cumsum()
    if s.empty:
        return 0.0
    s = pd.concat([pd.Series([0.0]), s], ignore_index=True)
    peak = s.cummax()
    drawdown = s - peak
    return float(drawdown.min())


def _sharpe(df: pd.DataFrame) -> Optional[float]:
    """Daily Sharpe, annualized with sqrt(252). Returns None if insufficient data."""
    daily = (df.assign(d=pd.to_datetime(df["closed_at"]).dt.floor("D"))
               .groupby("d")["pnl_usd"].sum().astype(float))
    if len(daily) < 2:
        return None
    std = daily.std(ddof=1)
    if std == 0 or math.isnan(std):
        return None
    return float(daily.mean() / std * math.sqrt(252))
