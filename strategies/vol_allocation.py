from __future__ import annotations

from typing import Optional

from strategies.base_strategy import BaseStrategy, SignalData

# Default regime-to-(allocation, leverage) mapping
_DEFAULT_ALLOCATION_MAP: dict[str, tuple[float, float]] = {
    "Euphoria": (0.50, 1.0),
    "Bull":     (0.95, 1.25),
    "Neutral":  (0.60, 1.0),
    "Bear":     (0.25, 1.0),
    "Crash":    (0.05, 1.0),
}

_DEFAULT_FALLBACK = (0.50, 1.0)  # Unknown / unrecognised regime

_UNCERTAINTY_CAP = 0.30


class VolatilityAllocationStrategy(BaseStrategy):
    """Regime-aware allocation strategy with confidence dampening.

    Key behaviours
    --------------
    * Confidence dampening: ``damped_allocation = base_allocation * confidence``
    * Uncertainty override: if ``high_uncertainty`` is True, cap allocation at 0.30
    * Stability gate: if ``stable`` is False, return last stable signal (or default)
    """

    def __init__(self, allocation_map: Optional[dict[str, tuple[float, float]]] = None) -> None:
        self._allocation_map: dict[str, tuple[float, float]] = {
            **_DEFAULT_ALLOCATION_MAP,
            **(allocation_map or {}),
        }
        self._last_stable_signals: dict[str, SignalData] = {}

    # ------------------------------------------------------------------
    # BaseStrategy interface
    # ------------------------------------------------------------------

    def name(self) -> str:
        return "VolatilityAllocationStrategy"

    def compute_signal(self, regime_result: dict, ticker: str = "_default") -> SignalData:
        """Translate a regime classification dict into a SignalData.

        Parameters
        ----------
        regime_result:
            Dict produced by ``RegimeClassifier.update()``.  Expected keys:
            ``regime``, ``confidence``, ``stable``, ``high_uncertainty``,
            ``state``.
        ticker:
            Asset identifier for per-ticker signal caching.
        """
        regime: str = regime_result["regime"]
        confidence: float = float(regime_result["confidence"])
        stable: bool = bool(regime_result["stable"])
        high_uncertainty: bool = bool(regime_result["high_uncertainty"])

        if not stable:
            cached = self._last_stable_signals.get(ticker)
            if cached is not None:
                return cached
            return SignalData(
                regime=regime,
                confidence=confidence,
                allocation_pct=_DEFAULT_FALLBACK[0],
                leverage=_DEFAULT_FALLBACK[1],
                high_uncertainty=high_uncertainty,
                stable=stable,
                notes="No stable signal yet; using safe default",
            )

        base_allocation, leverage = self._allocation_map.get(regime, _DEFAULT_FALLBACK)
        notes_parts: list[str] = []

        if regime not in self._allocation_map:
            notes_parts.append(f"Unrecognised regime '{regime}'; using safe default")

        damped_allocation = base_allocation * confidence
        notes_parts.append(
            f"base={base_allocation:.2f} * conf={confidence:.2f} -> damped={damped_allocation:.2f}"
        )

        if high_uncertainty and damped_allocation > _UNCERTAINTY_CAP:
            notes_parts.append(
                f"high_uncertainty cap applied: {damped_allocation:.2f} -> {_UNCERTAINTY_CAP:.2f}"
            )
            damped_allocation = _UNCERTAINTY_CAP

        if damped_allocation * leverage > 1.0:
            damped_allocation = 1.0 / leverage

        signal = SignalData(
            regime=regime,
            confidence=confidence,
            allocation_pct=damped_allocation,
            leverage=leverage,
            high_uncertainty=high_uncertainty,
            stable=stable,
            notes="; ".join(notes_parts),
        )

        self._last_stable_signals[ticker] = signal
        return signal
