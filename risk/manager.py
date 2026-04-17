"""
manager.py — Absolute Veto risk manager for the regime_trader system.

CRITICAL: This module checks for lock.file at import time. If the file exists,
the process is terminated immediately. This ensures an emergency-shutdown event
is never silently bypassed on restart.
"""

import os
import sys

if os.path.exists("lock.file"):
    print("SYSTEM HALTED: lock.file exists. A 10% drawdown event was detected.")
    print("Review the incident and manually delete lock.file to resume trading.")
    sys.exit(1)

# ---------------------------------------------------------------------------
# Standard imports (only reached if lock.file is absent)
# ---------------------------------------------------------------------------

import pandas as pd
import numpy as np
from typing import Optional

from risk.circuit_breakers import CircuitBreaker


class RiskManager:
    """
    Absolute Veto layer.  ALL position sizing and trade approval flows through here.

    Three sequential gates:
    1. Circuit breaker check  — may zero-out or halve the requested allocation.
    2. Max-risk-per-trade cap — no single position may exceed ``max_risk_per_trade``
       fraction of total portfolio equity.
    3. Correlation check      — reduces or rejects positions that are highly
       correlated with existing holdings.
    """

    def __init__(
        self,
        portfolio_equity: float,
        circuit_breaker: CircuitBreaker,
        max_risk_per_trade: float = 0.01,
    ):
        """
        Parameters
        ----------
        portfolio_equity  : float  — current total equity value of the portfolio.
        circuit_breaker   : CircuitBreaker — pre-constructed circuit breaker instance.
        max_risk_per_trade: float  — maximum fraction of portfolio equity that any
                                     single position may represent (default 1%).
        """
        self.portfolio_equity = portfolio_equity
        self.circuit_breaker = circuit_breaker
        self.max_risk_per_trade = max_risk_per_trade

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def approve_trade(
        self,
        ticker: str,
        proposed_allocation_pct: float,
        current_positions: dict,
        price_data: dict,
    ) -> dict:
        """
        Gate every trade through three risk checks.

        Parameters
        ----------
        ticker                  : str   — symbol of the instrument to trade.
        proposed_allocation_pct : float — requested allocation as a fraction of
                                          portfolio equity (e.g. 0.05 = 5%).
        current_positions       : dict[str, float] — mapping of ticker -> current
                                  allocation fraction for existing positions.
        price_data              : dict[str, pd.Series] — mapping of ticker ->
                                  price series (used for correlation computation).

        Returns
        -------
        dict with keys:
            approved               : bool
            ticker                 : str
            approved_allocation_pct: float
            rejection_reason       : str   — empty string when approved
            circuit_level          : int
            correlation_warning    : bool
        """
        result = {
            "approved": False,
            "ticker": ticker,
            "approved_allocation_pct": 0.0,
            "rejection_reason": "",
            "circuit_level": 0,
            "correlation_warning": False,
        }

        # ------------------------------------------------------------------
        # Gate 1: Circuit breaker
        # ------------------------------------------------------------------
        # We call check() with current equity and a daily_pnl_pct of 0.0 here
        # because the caller is responsible for passing daily P&L context.
        # The daily P&L is tracked externally; we only apply the multiplier that
        # the circuit breaker would currently assign based on its last known state.
        # For a conservative default, derive daily_pnl_pct from equity vs. peak.
        daily_pnl_pct = 0.0  # caller should update equity before approving trades
        cb_status = self.circuit_breaker.check(self.portfolio_equity, daily_pnl_pct)
        result["circuit_level"] = cb_status["level"]

        if cb_status["trading_suspended"]:
            result["rejection_reason"] = (
                f"Trading suspended by circuit breaker: {cb_status['action']}"
            )
            return result

        multiplier = cb_status["multiplier"]
        approved_allocation_pct = proposed_allocation_pct * multiplier

        # ------------------------------------------------------------------
        # Gate 2: Max risk per trade (1% cap)
        # ------------------------------------------------------------------
        approved_dollar = min(
            approved_allocation_pct * self.portfolio_equity,
            self.max_risk_per_trade * self.portfolio_equity,
        )
        approved_allocation_pct = approved_dollar / self.portfolio_equity

        # ------------------------------------------------------------------
        # Gate 3: Correlation check
        # ------------------------------------------------------------------
        max_corr = self.compute_correlation(ticker, current_positions, price_data)

        if max_corr > 0.95:
            result["rejection_reason"] = (
                f"Correlation too high ({max_corr:.3f} > 0.95) with existing positions."
            )
            result["correlation_warning"] = True
            return result

        if max_corr > 0.70:
            result["correlation_warning"] = True
            approved_allocation_pct = approved_allocation_pct * (1 - max_corr)

        # ------------------------------------------------------------------
        # All gates passed
        # ------------------------------------------------------------------
        result["approved"] = True
        result["approved_allocation_pct"] = approved_allocation_pct
        return result

    def update_equity(self, new_equity: float):
        """
        Update the tracked portfolio equity and propagate to the circuit breaker's
        peak high-water mark.
        """
        self.portfolio_equity = new_equity
        self.circuit_breaker.update_peak(new_equity)

    def compute_correlation(
        self,
        ticker: str,
        current_positions: dict,
        price_data: dict,
    ) -> float:
        """
        Compute the maximum pairwise Pearson correlation between ``ticker``'s
        return series and each existing position's return series.

        Parameters
        ----------
        ticker            : str — the candidate instrument.
        current_positions : dict[str, float] — existing position tickers as keys.
        price_data        : dict[str, pd.Series] — price series keyed by ticker.

        Returns
        -------
        float
            Maximum absolute Pearson correlation found.  Returns 0.0 if there are
            no existing positions or if the candidate ticker has no price data.
        """
        if not current_positions:
            return 0.0

        if ticker not in price_data:
            return 0.0

        candidate_prices = price_data[ticker]
        if len(candidate_prices) < 2:
            return 0.0

        candidate_returns = candidate_prices.pct_change().dropna()

        max_corr = 0.0
        for pos_ticker in current_positions:
            if pos_ticker == ticker:
                continue
            if pos_ticker not in price_data:
                continue

            pos_prices = price_data[pos_ticker]
            if len(pos_prices) < 2:
                continue

            pos_returns = pos_prices.pct_change().dropna()

            # Align on overlapping index
            aligned = pd.concat(
                [candidate_returns.rename("candidate"), pos_returns.rename("pos")],
                axis=1,
                join="inner",
            ).dropna()

            if len(aligned) < 2:
                continue

            corr = aligned["candidate"].corr(aligned["pos"])
            if pd.isna(corr):
                continue

            abs_corr = abs(corr)
            if abs_corr > max_corr:
                max_corr = abs_corr

        return max_corr
