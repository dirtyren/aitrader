"""Tests for strategies.vol_allocation.VolatilityAllocationStrategy."""

import os
import pytest

os.environ.setdefault("TRADING_ENV", "test")

from strategies.vol_allocation import VolatilityAllocationStrategy


def _regime_result(regime="Bull", confidence=0.8, stable=True, high_uncertainty=False):
    return {
        "regime": regime,
        "confidence": confidence,
        "stable": stable,
        "high_uncertainty": high_uncertainty,
        "state": 0,
    }


class TestComputeSignal:
    def test_bull_regime(self):
        s = VolatilityAllocationStrategy()
        signal = s.compute_signal(_regime_result("Bull", confidence=0.9))
        # 0.95 * 0.9 = 0.855, but 0.855 * 1.25 = 1.069 > 1.0, so capped to 1.0/1.25 = 0.8
        assert signal.allocation_pct == pytest.approx(0.8)
        assert signal.leverage == 1.25

    def test_crash_regime_low_allocation(self):
        s = VolatilityAllocationStrategy()
        signal = s.compute_signal(_regime_result("Crash", confidence=0.9))
        assert signal.allocation_pct == pytest.approx(0.05 * 0.9)
        assert signal.leverage == 1.0

    def test_bear_regime(self):
        s = VolatilityAllocationStrategy()
        signal = s.compute_signal(_regime_result("Bear", confidence=1.0))
        assert signal.allocation_pct == pytest.approx(0.25)

    def test_confidence_dampening(self):
        s = VolatilityAllocationStrategy()
        full = s.compute_signal(_regime_result("Bull", confidence=1.0))
        half = s.compute_signal(_regime_result("Bull", confidence=0.5))
        assert half.allocation_pct < full.allocation_pct

    def test_uncertainty_cap(self):
        s = VolatilityAllocationStrategy()
        signal = s.compute_signal(_regime_result("Bull", confidence=1.0, high_uncertainty=True))
        assert signal.allocation_pct <= 0.30

    def test_leverage_cap(self):
        s = VolatilityAllocationStrategy()
        signal = s.compute_signal(_regime_result("Bull", confidence=1.0))
        assert signal.allocation_pct * signal.leverage <= 1.0

    def test_unknown_regime_uses_fallback(self):
        s = VolatilityAllocationStrategy()
        signal = s.compute_signal(_regime_result("SomethingNew", confidence=0.8))
        assert signal.allocation_pct == pytest.approx(0.50 * 0.8)


class TestStabilityGate:
    def test_unstable_returns_default_when_no_cache(self):
        s = VolatilityAllocationStrategy()
        signal = s.compute_signal(_regime_result("Bull", stable=False))
        assert signal.allocation_pct == 0.50

    def test_unstable_returns_cached_stable_signal(self):
        s = VolatilityAllocationStrategy()
        stable = s.compute_signal(_regime_result("Bull", confidence=0.9, stable=True), ticker="SPY")
        unstable = s.compute_signal(_regime_result("Crash", confidence=0.5, stable=False), ticker="SPY")
        assert unstable.allocation_pct == stable.allocation_pct


class TestPerTickerCache:
    def test_separate_caches_per_ticker(self):
        s = VolatilityAllocationStrategy()
        s.compute_signal(_regime_result("Bull", confidence=0.9, stable=True), ticker="SPY")
        s.compute_signal(_regime_result("Bear", confidence=0.8, stable=True), ticker="IWM")

        spy_unstable = s.compute_signal(_regime_result("Crash", stable=False), ticker="SPY")
        iwm_unstable = s.compute_signal(_regime_result("Crash", stable=False), ticker="IWM")

        assert spy_unstable.regime == "Bull"
        assert iwm_unstable.regime == "Bear"
