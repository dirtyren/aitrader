"""
RegimeClassifier — stability filter and uncertainty detector on top of HMMModel.

Wraps an HMMModel instance and maintains a rolling history of raw states to:
  1. Gate regime changes behind a persistence filter (last-3-same → stable).
  2. Detect high-uncertainty periods (>4 transitions in last 20 bars).
"""

from __future__ import annotations

import logging
from collections import deque
from typing import Optional

import numpy as np

from engine.hmm_model import HMMModel


class RegimeClassifier:
    """Stateful wrapper around :class:`HMMModel` that adds stability filtering.

    Parameters
    ----------
    hmm_model:
        A fitted :class:`HMMModel` instance.
    history_len:
        Maximum number of recent states to retain (default 20).

    Attributes
    ----------
    history : deque[int]
        Rolling window of raw integer states (maxlen=20).
    stable_regime : str | None
        Last confirmed stable regime name (set only when the last 3 states
        are identical).
    """

    def __init__(self, hmm_model: HMMModel, history_len: int = 20) -> None:
        self.hmm_model = hmm_model
        self.history: deque[int] = deque(maxlen=history_len)
        self.stable_regime: Optional[str] = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def update(
        self,
        observations: np.ndarray,
        logger: Optional[logging.Logger] = None,
    ) -> dict:
        """Process the latest observations and return a classification dict.

        Parameters
        ----------
        observations:
            2-D numpy array of shape ``(T, n_features)`` — the most recent
            bars fed into the HMM.
        logger:
            Optional :class:`logging.Logger` for uncertainty warnings.

        Returns
        -------
        dict with keys:
            ``regime``          – human-readable regime name (str)
            ``state``           – raw integer state (int)
            ``confidence``      – probability of the raw state (float)
            ``stable``          – True if the last 3 states agree (bool)
            ``high_uncertainty``– True if >4 transitions in last 20 bars (bool)
        """
        raw_state, state_probs = self.hmm_model.predict_current_regime(observations)
        regime_name = self.hmm_model.get_regime_name(raw_state)
        confidence = float(state_probs[raw_state])

        self.history.append(raw_state)

        stable = self._check_persistence()
        high_uncertainty = self._check_uncertainty(logger=logger)

        return {
            "regime": regime_name,
            "state": raw_state,
            "confidence": confidence,
            "stable": stable,
            "high_uncertainty": high_uncertainty,
        }

    def get_stable_regime(self) -> Optional[str]:
        """Return the last confirmed stable regime name, or None."""
        return self.stable_regime

    # ------------------------------------------------------------------
    # Internal checks
    # ------------------------------------------------------------------

    def _check_persistence(self) -> bool:
        """Return True if the last 3 history entries are all the same state.

        When True, ``self.stable_regime`` is updated to the current regime name.
        """
        if len(self.history) < 3:
            return False

        last_three = list(self.history)[-3:]
        if last_three[0] == last_three[1] == last_three[2]:
            self.stable_regime = self.hmm_model.get_regime_name(last_three[-1])
            return True

        return False

    def _check_uncertainty(
        self,
        logger: Optional[logging.Logger] = None,
    ) -> bool:
        """Return True if the number of regime transitions in history exceeds 4.

        A transition is counted each time consecutive history entries differ.

        Parameters
        ----------
        logger:
            If provided, a WARNING is emitted when uncertainty is high.
        """
        history_list = list(self.history)
        if len(history_list) < 2:
            return False

        transitions = sum(
            1
            for i in range(1, len(history_list))
            if history_list[i] != history_list[i - 1]
        )

        history_snapshot = history_list
        # threshold = 4 transitions per 20 bars (spec requirement)
        threshold = max(4, len(history_snapshot) // 5)
        if transitions > threshold:
            if logger is not None:
                logger.warning(
                    "HIGH_UNCERTAINTY: regime changed %d times in last 20 bars",
                    transitions,
                )
            return True

        return False


# ---------------------------------------------------------------------------
# Quick smoke-test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # Verify that RegimeClassifier can be instantiated and run end-to-end
    # using a synthetic dataset (no live data required).
    import pandas as pd

    print("Building synthetic feature data …")
    rng = np.random.default_rng(0)
    n_rows = 504  # ~2 years of daily data

    df = pd.DataFrame(
        {
            "log_return": rng.normal(0.0002, 0.01, n_rows),
            "volatility": np.abs(rng.normal(0.01, 0.002, n_rows)),
            "volume_change": rng.normal(0.0, 0.05, n_rows),
        }
    )

    print("Fitting HMMModel …")
    hmm = HMMModel()
    hmm.fit(df)
    print(f"  Selected n_regimes = {hmm.n_regimes}")
    print(f"  Regime labels      = {hmm.regime_labels}")

    print("Instantiating RegimeClassifier …")
    clf = RegimeClassifier(hmm)

    # Feed the last 60 bars as "recent observations"
    obs = df[["log_return", "volatility", "volume_change"]].values[-60:]

    result = clf.update(obs)
    print(f"  update() result = {result}")
    print(f"  stable_regime   = {clf.get_stable_regime()}")

    # Simulate streaming 25 individual bars to exercise persistence/uncertainty
    for i in range(25):
        single_bar = df[["log_return", "volatility", "volume_change"]].values[i : i + 60]
        clf.update(single_bar)

    print(f"  stable_regime after 25 updates = {clf.get_stable_regime()}")
    print("Smoke-test PASSED.")
