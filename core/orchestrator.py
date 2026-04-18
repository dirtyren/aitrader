from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING, Optional

import numpy as np

from engine.hmm_model import HMMModel
from engine.regime_classifier import RegimeClassifier
from strategies.base_strategy import BaseStrategy, SignalData

if TYPE_CHECKING:
    from core.portfolio import Portfolio


class StrategyOrchestrator:
    """Central coordinator between HMM output and the order executor.

    Wraps a fitted HMMModel + RegimeClassifier pair and a strategy, exposing
    a single :meth:`process` entry-point that returns a :class:`SignalData`.

    Parameters
    ----------
    hmm_model:
        A fitted :class:`~engine.hmm_model.HMMModel` instance.
    regime_classifier:
        A :class:`~engine.regime_classifier.RegimeClassifier` wrapping *hmm_model*.
    strategy:
        Any :class:`~strategies.base_strategy.BaseStrategy` implementation.
    logger:
        Optional :class:`logging.Logger`.  If omitted a module-level logger is
        used (``regime_trader.orchestrator``).
    """

    def __init__(
        self,
        hmm_model: HMMModel,
        regime_classifier: RegimeClassifier,
        strategy: BaseStrategy,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self.hmm_model = hmm_model
        self.regime_classifier = regime_classifier
        self.strategy = strategy
        self.logger = logger or logging.getLogger("regime_trader.orchestrator")
        self._last_regime: Optional[str] = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def process(self, observations: np.ndarray) -> SignalData:
        """Run one end-to-end cycle and return a trading signal.

        Steps
        -----
        1. Call ``regime_classifier.update(observations)`` to get a
           ``regime_result`` dict.
        2. Pass ``regime_result`` to ``strategy.compute_signal()`` to get a
           :class:`SignalData`.
        3. Log a regime-change message if the regime name differs from the
           previous call.
        4. Return the :class:`SignalData`.

        Parameters
        ----------
        observations:
            2-D numpy array of shape ``(T, n_features)`` — the most recent
            bars fed into the HMM.
        """
        regime_result: dict = self.regime_classifier.update(
            observations, logger=self.logger
        )

        signal: SignalData = self.strategy.compute_signal(regime_result)

        # --- Regime-change detection & logging -----------------------
        with self._lock:
            old_regime = self._last_regime
            if old_regime is not None and old_regime != regime_result["regime"]:
                self.logger.info(
                    "REGIME_CHANGE: %s -> %s | confidence=%.2f | stable=%s | alloc=%.2f",
                    old_regime,
                    regime_result["regime"],
                    regime_result["confidence"],
                    regime_result["stable"],
                    signal.allocation_pct,
                )
            self._last_regime = regime_result["regime"]

        return signal

    def process_portfolio(
        self,
        observations_map: dict[str, np.ndarray],
        portfolio: "Portfolio",
    ) -> dict[str, SignalData]:
        """
        Run regime detection + signal generation for each asset in the portfolio.

        observations_map: {ticker: np.ndarray of features (log_return, volatility, volume_change)}

        For each ticker in portfolio.tickers:
          - If observations available: call self.process(observations) to get SignalData
          - If observations missing: return a safe default SignalData
            (regime="Unknown", confidence=0.0, allocation_pct=0.5, leverage=1.0, stable=False, high_uncertainty=True)

        Returns {ticker: SignalData}
        """
        results: dict[str, SignalData] = {}
        for ticker in portfolio.tickers:
            if ticker in observations_map:
                results[ticker] = self.process(observations_map[ticker])
            else:
                self.logger.warning(
                    "PROCESS_PORTFOLIO: no observations for %s — using safe default", ticker
                )
                results[ticker] = SignalData(
                    regime="Unknown",
                    confidence=0.0,
                    allocation_pct=0.5,
                    leverage=1.0,
                    stable=False,
                    high_uncertainty=True,
                )
        return results
