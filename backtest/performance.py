"""
PerformanceEngine — vectorized backtesting with slippage/commission accounting.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


class PerformanceEngine:
    """Vectorized backtesting engine.

    Parameters
    ----------
    slippage_bps:
        One-way slippage in basis points per trade (default 5.0 bps).
    commission_bps:
        One-way commission in basis points per trade (default 2.0 bps).
    """

    def __init__(self, slippage_bps: float = 5.0, commission_bps: float = 2.0) -> None:
        self.slippage_bps = slippage_bps
        self.commission_bps = commission_bps

    # ------------------------------------------------------------------
    # Core backtest
    # ------------------------------------------------------------------

    def run(self, price_series: pd.Series, signals: pd.Series) -> dict:
        """Vectorized backtest.

        Parameters
        ----------
        price_series:
            Daily close prices indexed by date.
        signals:
            Daily allocation fractions (0.0–1.0) aligned with price_series.

        Returns
        -------
        dict with keys:
            total_return, sharpe_ratio, max_drawdown, calmar_ratio,
            daily_returns (pd.Series)
        """
        prices = price_series.copy()
        sigs = signals.reindex(prices.index).fillna(0.0)

        # 1. Daily log returns
        log_rets = np.log(prices / prices.shift(1))

        # 2. Lag signals by 1 bar to prevent look-ahead bias
        sigs_lagged = sigs.shift(1).fillna(0.0)

        # 3. Strategy log returns (allocation * log_return)
        strat_log_rets = sigs_lagged * log_rets

        # 4. Transaction cost proportional to turnover
        cost_per_unit = (self.slippage_bps + self.commission_bps) / 10_000.0
        position_changes = sigs_lagged.diff().abs()
        position_changes.iloc[0] = sigs_lagged.iloc[0].abs()
        trade_costs = position_changes * cost_per_unit

        # Net daily log returns after costs
        net_log_rets = strat_log_rets - trade_costs

        # Drop the first NaN from the return calculation
        net_log_rets = net_log_rets.dropna()

        # 5. Cumulative returns (arithmetic for P&L reporting)
        cum_returns = net_log_rets.cumsum().apply(np.exp) - 1.0

        # 6. Metrics
        total_return = float(cum_returns.iloc[-1]) if len(cum_returns) > 0 else 0.0

        sharpe_ratio = self._compute_sharpe(net_log_rets)
        max_drawdown = self._compute_max_drawdown(net_log_rets)
        calmar_ratio = (
            total_return / abs(max_drawdown) if max_drawdown != 0.0 else np.nan
        )

        return {
            "total_return": total_return,
            "sharpe_ratio": sharpe_ratio,
            "max_drawdown": max_drawdown,
            "calmar_ratio": calmar_ratio,
            "daily_returns": net_log_rets,
        }

    # ------------------------------------------------------------------
    # Regime bucket analysis
    # ------------------------------------------------------------------

    def regime_bucket_analysis(
        self,
        daily_returns: pd.Series,
        regime_series: pd.Series,
    ) -> pd.DataFrame:
        """Group daily_returns by regime label and compute per-regime stats.

        Parameters
        ----------
        daily_returns:
            Net daily log returns from :meth:`run`.
        regime_series:
            Series of regime label strings aligned with daily_returns.

        Returns
        -------
        pd.DataFrame indexed by regime with columns:
            mean_return, std_return, sharpe, total_return, count
        """
        combined = pd.DataFrame(
            {"returns": daily_returns, "regime": regime_series}
        ).dropna(subset=["regime"])

        rows = []
        for regime, grp in combined.groupby("regime"):
            rets = grp["returns"].dropna()
            n = len(rets)
            mean_r = float(rets.mean()) if n > 0 else 0.0
            std_r = float(rets.std()) if n > 1 else 0.0
            sharpe = (
                float(np.sqrt(252) * mean_r / std_r)
                if std_r > 0
                else np.nan
            )
            total_r = float(np.exp(rets.sum()) - 1.0)
            rows.append(
                {
                    "regime": regime,
                    "mean_return": mean_r,
                    "std_return": std_r,
                    "sharpe": sharpe,
                    "total_return": total_r,
                    "count": n,
                }
            )

        if not rows:
            return pd.DataFrame(
                columns=["regime", "mean_return", "std_return", "sharpe", "total_return", "count"]
            ).set_index("regime")

        df = pd.DataFrame(rows).set_index("regime")
        return df

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _compute_sharpe(daily_log_rets: pd.Series) -> float:
        """Annualized Sharpe ratio (sqrt(252) * mean / std)."""
        rets = daily_log_rets.dropna()
        if len(rets) < 2:
            return np.nan
        mean_r = float(rets.mean())
        std_r = float(rets.std())
        if std_r == 0.0:
            return np.nan
        return float(np.sqrt(252) * mean_r / std_r)

    @staticmethod
    def _compute_max_drawdown(daily_log_rets: pd.Series) -> float:
        """Peak-to-valley max drawdown on cumulative return series."""
        rets = daily_log_rets.dropna()
        if len(rets) == 0:
            return 0.0
        cum = rets.cumsum().apply(np.exp)
        peak = cum.cummax()
        drawdown = (cum - peak) / peak
        return float(drawdown.min())
