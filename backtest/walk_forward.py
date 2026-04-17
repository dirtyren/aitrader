"""
WalkForwardBacktest — rolling train/test walk-forward framework.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from backtest.performance import PerformanceEngine
from engine.regime_classifier import RegimeClassifier


def _build_features(price_series: pd.Series) -> pd.DataFrame:
    """Compute HMM input features from a price series.

    Returns
    -------
    pd.DataFrame with columns: log_return, volatility, volume_change
    """
    log_return = np.log(price_series / price_series.shift(1))
    volatility = log_return.rolling(window=20, min_periods=1).std()
    volume_change = pd.Series(0.0, index=price_series.index)

    return pd.DataFrame(
        {
            "log_return": log_return,
            "volatility": volatility,
            "volume_change": volume_change,
        }
    )


class WalkForwardBacktest:
    """Rolling walk-forward backtest over a price series.

    Parameters
    ----------
    train_days:
        Number of bars used to fit the HMM on each fold (default 252).
    test_days:
        Number of bars used for out-of-sample evaluation (default 126).
        Windows are non-overlapping: each fold slides forward by test_days.
    """

    def __init__(self, train_days: int = 252, test_days: int = 126) -> None:
        self.train_days = train_days
        self.test_days = test_days
        self._engine = PerformanceEngine()

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    def run(self, price_series: pd.Series, hmm_model, strategy) -> list[dict]:
        """Roll a walk-forward window across price_series.

        Parameters
        ----------
        price_series:
            Daily close prices as a pd.Series (DatetimeIndex recommended).
        hmm_model:
            An unfitted (or re-fittable) HMMModel instance.  :meth:`fit` will
            be called on each training window.
        strategy:
            A BaseStrategy instance whose ``compute_signal`` will be called
            for each bar in the test window.

        Returns
        -------
        List of dicts, one per fold, with keys:
            window_id, train_start, train_end, test_start, test_end,
            total_return, sharpe_ratio, max_drawdown, regime_breakdown
        """
        features = _build_features(price_series)
        prices = price_series.dropna()
        n = len(prices)
        results: list[dict] = []
        window_id = 0

        start = 0
        while start + self.train_days + self.test_days <= n:
            train_end_idx = start + self.train_days
            test_end_idx = train_end_idx + self.test_days

            train_prices = prices.iloc[start:train_end_idx]
            test_prices = prices.iloc[train_end_idx:test_end_idx]

            train_features = features.loc[train_prices.index].dropna()
            test_features = features.loc[test_prices.index]

            # 1. Fit HMM on training window
            hmm_model.fit(train_features)
            classifier = RegimeClassifier(hmm_model)

            # 2. Generate signals over the test window bar-by-bar
            signals, regimes = self._generate_signals(
                test_features, test_prices, classifier, strategy
            )

            # 3. Run PerformanceEngine on test window
            perf = self._engine.run(test_prices, signals)

            # 4. Regime breakdown
            regime_breakdown = self._regime_breakdown(perf["daily_returns"], regimes)

            results.append(
                {
                    "window_id": window_id,
                    "train_start": train_prices.index[0],
                    "train_end": train_prices.index[-1],
                    "test_start": test_prices.index[0],
                    "test_end": test_prices.index[-1],
                    "total_return": perf["total_return"],
                    "sharpe_ratio": perf["sharpe_ratio"],
                    "max_drawdown": perf["max_drawdown"],
                    "regime_breakdown": regime_breakdown,
                }
            )

            window_id += 1
            start += self.test_days  # non-overlapping OOS windows

        return results

    # ------------------------------------------------------------------
    # Summary helper
    # ------------------------------------------------------------------

    def summary(self, results: list[dict]) -> pd.DataFrame:
        """Convert list of window dicts to a summary DataFrame."""
        if not results:
            return pd.DataFrame()

        rows = []
        for r in results:
            rows.append(
                {
                    "window_id": r["window_id"],
                    "train_start": r["train_start"],
                    "train_end": r["train_end"],
                    "test_start": r["test_start"],
                    "test_end": r["test_end"],
                    "total_return": r["total_return"],
                    "sharpe_ratio": r["sharpe_ratio"],
                    "max_drawdown": r["max_drawdown"],
                }
            )

        return pd.DataFrame(rows).set_index("window_id")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _generate_signals(
        self,
        test_features: pd.DataFrame,
        test_prices: pd.Series,
        classifier: RegimeClassifier,
        strategy,
    ) -> tuple[pd.Series, pd.Series]:
        """Generate per-bar allocation signals over the test window.

        Uses a causal expanding window of test features so no future data
        leaks into each bar's regime estimate.

        Returns
        -------
        signals : pd.Series — allocation_pct per date
        regimes : pd.Series — regime label per date
        """
        allocations: dict = {}
        regime_labels: dict = {}

        feat_values = test_features[["log_return", "volatility", "volume_change"]].fillna(0.0)

        for i, date in enumerate(test_prices.index):
            # Use all test bars up to and including the current one
            obs_slice = feat_values.iloc[: i + 1].values
            if len(obs_slice) == 0:
                allocations[date] = 0.0
                regime_labels[date] = "Unknown"
                continue

            regime_result = classifier.update(obs_slice)
            signal = strategy.compute_signal(regime_result)
            allocations[date] = signal.allocation_pct
            regime_labels[date] = regime_result["regime"]

        signals = pd.Series(allocations, name="allocation")
        regimes = pd.Series(regime_labels, name="regime")
        return signals, regimes

    @staticmethod
    def _regime_breakdown(daily_returns: pd.Series, regimes: pd.Series) -> dict:
        """Compute mean return per regime over the test window."""
        combined = pd.DataFrame(
            {"returns": daily_returns, "regime": regimes}
        ).dropna(subset=["regime"])

        breakdown: dict = {}
        for regime, grp in combined.groupby("regime"):
            breakdown[regime] = {
                "mean_return": float(grp["returns"].mean()),
                "count": int(len(grp)),
            }
        return breakdown
