"""Tests for risk.circuit_breakers.CircuitBreaker."""

import os
import pytest

os.environ.setdefault("TRADING_ENV", "test")

from risk.circuit_breakers import CircuitBreaker, TradingSuspension


class TestCircuitBreakerLevels:
    def _make(self, peak=100_000.0):
        return CircuitBreaker(
            peak_equity=peak,
            daily_loss_limit_1=0.02,
            daily_loss_limit_2=0.03,
            drawdown_limit=0.10,
        )

    def test_level0_clear(self):
        cb = self._make()
        result = cb.check(100_000.0, daily_pnl_pct=0.0)
        assert result["level"] == 0
        assert result["multiplier"] == 1.0
        assert not result["trading_suspended"]

    def test_level1_reduce(self):
        cb = self._make()
        result = cb.check(100_000.0, daily_pnl_pct=-0.02)
        assert result["level"] == 1
        assert result["multiplier"] == 0.5
        assert not result["trading_suspended"]

    def test_level2_halt(self):
        cb = self._make()
        result = cb.check(100_000.0, daily_pnl_pct=-0.03)
        assert result["level"] == 2
        assert result["multiplier"] == 0.0
        assert result["trading_suspended"]

    def test_level3_emergency_shutdown(self):
        cb = self._make(peak=100_000.0)
        with pytest.raises(SystemExit):
            cb.check(89_000.0, daily_pnl_pct=0.0)

    def test_level1_boundary_not_triggered(self):
        cb = self._make()
        result = cb.check(100_000.0, daily_pnl_pct=-0.019)
        assert result["level"] == 0

    def test_level2_boundary_not_triggered(self):
        cb = self._make()
        result = cb.check(100_000.0, daily_pnl_pct=-0.029)
        assert result["level"] == 1


class TestPeakUpdate:
    def test_update_peak_new_high(self):
        cb = CircuitBreaker(peak_equity=100_000.0)
        cb.update_peak(110_000.0)
        assert cb.peak_equity == 110_000.0

    def test_update_peak_lower_ignored(self):
        cb = CircuitBreaker(peak_equity=100_000.0)
        cb.update_peak(90_000.0)
        assert cb.peak_equity == 100_000.0

    def test_drawdown_calculation(self):
        cb = CircuitBreaker(peak_equity=100_000.0)
        assert cb.peak_to_valley_drawdown(95_000.0) == pytest.approx(0.05)
        assert cb.peak_to_valley_drawdown(100_000.0) == pytest.approx(0.0)


class TestTradingSuspension:
    def test_not_suspended_by_default(self):
        ts = TradingSuspension()
        assert not ts.is_active()

    def test_suspended_active(self):
        ts = TradingSuspension(suspended=True)
        assert ts.is_active()

    def test_halt_24h_factory(self):
        ts = TradingSuspension.halt_24h("test reason")
        assert ts.is_active()
        assert ts.reason == "test reason"
