"""
HMMModel — Gaussian HMM regime detector with BIC-based model selection.

Input features (computed externally by feature_eng.py):
    - log_return:     daily log return
    - volatility:     rolling 20-day std of log returns
    - volume_change:  pct change in daily volume
"""

from __future__ import annotations

import warnings
import numpy as np
import pandas as pd
import joblib
from hmmlearn.hmm import GaussianHMM
from sklearn.preprocessing import StandardScaler


# ---------------------------------------------------------------------------
# Regime label maps for well-known component counts
# ---------------------------------------------------------------------------

_LABEL_MAP_3 = {0: "Bear", 1: "Neutral", 2: "Bull"}
_LABEL_MAP_5 = {0: "Crash", 1: "Bear", 2: "Neutral", 3: "Bull", 4: "Euphoria"}


class HMMModel:
    """Gaussian HMM wrapper with BIC-optimised component selection.

    Attributes
    ----------
    model : GaussianHMM | None
        The fitted hmmlearn model after calling :meth:`fit`.
    n_regimes : int | None
        Number of hidden states chosen by BIC minimisation.
    regime_labels : dict[int, str]
        Mapping from integer state to human-readable regime name.
    """

    def __init__(self) -> None:
        self.model: GaussianHMM | None = None
        self.n_regimes: int | None = None
        self.regime_labels: dict[int, str] = {}
        self._scaler: StandardScaler | None = None

    # ------------------------------------------------------------------
    # Fitting
    # ------------------------------------------------------------------

    def fit(self, features: pd.DataFrame) -> "HMMModel":
        """Fit a GaussianHMM selecting n_components by BIC minimisation.

        Parameters
        ----------
        features:
            DataFrame with columns ``log_return``, ``volatility``,
            ``volume_change``.  Typically 2 years of daily bars (~504 rows).

        Returns
        -------
        self
        """
        X_raw = features[["log_return", "volatility", "volume_change"]].dropna().values
        n_samples, n_features = X_raw.shape

        # Standardize features for numerical stability of the EM algorithm
        scaler = StandardScaler()
        X = scaler.fit_transform(X_raw)

        best_bic = np.inf
        best_model = None
        best_n = None
        best_scaler = None

        for n in range(3, 8):  # 3 to 7 inclusive
            try:
                model = GaussianHMM(
                    n_components=n,
                    covariance_type="full",
                    n_iter=100,
                    random_state=42,
                )
                with warnings.catch_warnings(record=True):
                    warnings.simplefilter("always")
                    model.fit(X)
                log_likelihood = model.score(X) * n_samples

                # n_params formula for full-covariance HMM:
                # transition: n*(n-1), initial: (n-1), means: n*d,
                # full covariance: n*d*(d+1)//2
                n_params = n * (n - 1) + (n - 1) + n * n_features + n * n_features * (n_features + 1) // 2
                bic = -2.0 * log_likelihood + n_params * np.log(n_samples)

                if bic < best_bic:
                    best_bic = bic
                    best_model = model
                    best_n = n
                    best_scaler = scaler
            except (ValueError, np.linalg.LinAlgError):
                # hmmlearn can occasionally fail to converge; skip this n
                continue

        if best_model is None:
            raise RuntimeError("HMM fitting failed for all candidate n_components.")

        self.model = best_model
        self.n_regimes = best_n
        self._scaler = best_scaler
        self._build_regime_labels()
        return self

    # ------------------------------------------------------------------
    # Inference
    # ------------------------------------------------------------------

    def predict_current_regime(
        self, observations: np.ndarray
    ) -> tuple[int, np.ndarray]:
        """Return the current (most recent) regime state and its probability vector.

        Uses the Viterbi / Forward-Backward algorithm as exposed by
        ``GaussianHMM.predict`` — no future look-ahead.

        Parameters
        ----------
        observations:
            2-D array of shape ``(T, n_features)`` representing the most
            recent T bars.

        Returns
        -------
        raw_state : int
            Integer state index for the current bar.
        state_probs : np.ndarray
            Probability vector of shape ``(n_regimes,)`` for the current bar.
        """
        if self.model is None:
            raise RuntimeError("Model is not fitted. Call fit() first.")

        X = self._scaler.transform(observations) if self._scaler is not None else observations
        # Causal Forward Algorithm: only conditions on observations up to time T
        log_frameprob = self.model._compute_log_likelihood(X)
        alphas, _ = self.model._do_forward_pass(log_frameprob)
        # Normalize the last alpha vector to get state probabilities at time T
        last_alpha = np.exp(alphas[-1])
        state_probs = last_alpha / last_alpha.sum()
        raw_state = int(np.argmax(state_probs))
        return raw_state, state_probs

    def get_state_probabilities(self, observations: np.ndarray) -> np.ndarray:
        """Return the probability vector for the most recent bar.

        Parameters
        ----------
        observations:
            2-D array of shape ``(T, n_features)``.

        Returns
        -------
        np.ndarray of shape ``(n_regimes,)``
        """
        if self.model is None:
            raise RuntimeError("Model is not fitted. Call fit() first.")
        X = self._scaler.transform(observations) if self._scaler is not None else observations
        return self.model.predict_proba(X)[-1]

    # ------------------------------------------------------------------
    # Regime labeling
    # ------------------------------------------------------------------

    def label_regimes(self) -> dict[int, str]:
        """Build and return the regime-label mapping based on mean log_return.

        States are sorted by their mean log_return (ascending), then mapped
        to descriptive names:

        * 3 states  → Bear / Neutral / Bull
        * 5 states  → Crash / Bear / Neutral / Bull / Euphoria
        * otherwise → Regime_0 … Regime_N

        Also stored as ``self.regime_labels``.
        """
        self._build_regime_labels()
        return self.regime_labels

    def get_regime_name(self, state_int: int) -> str:
        """Return the human-readable name for ``state_int``.

        Parameters
        ----------
        state_int:
            Raw integer state returned by the HMM.
        """
        return self.regime_labels.get(state_int, f"Unknown_{state_int}")

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def save(self, path: str) -> None:
        """Persist the fitted model to *path* using joblib."""
        joblib.dump(self, path)

    @classmethod
    def load(cls, path: str) -> "HMMModel":
        """Load a previously saved :class:`HMMModel` from *path*."""
        return joblib.load(path)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _build_regime_labels(self) -> None:
        """Construct ``self.regime_labels`` from the fitted model's means."""
        if self.model is None:
            return

        n = self.model.n_components
        # Sort states by mean log_return (index 0 in the feature vector)
        mean_returns = self.model.means_[:, 0]
        sorted_idx = np.argsort(mean_returns)  # ascending: worst → best

        if n == 3:
            name_map = _LABEL_MAP_3
        elif n == 5:
            name_map = _LABEL_MAP_5
        else:
            name_map = {rank: f"Regime_{rank}" for rank in range(n)}

        self.regime_labels = {
            int(sorted_idx[rank]): name_map[rank] for rank in range(n)
        }
