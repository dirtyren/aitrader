"""Tests for risk.manager.RiskManager."""

import os
import pytest
import pandas as pd

os.environ.setdefault("TRADING_ENV", "test")

from risk.circuit_breakers import CircuitBreaker
from risk.manager import RiskManager


def _make_rm(equity=100_000.0, max_risk=0.01, max_rebalance=0.25):
    cb = CircuitBreaker(
        peak_equity=equity,
        daily_loss_limit_1=0.02,
        daily_loss_limit_2=0.03,
        drawdown_limit=0.10,
    )
    return RiskManager(
        portfolio_equity=equity,
        circuit_breaker=cb,
        max_risk_per_trade=max_risk,
        max_rebalance_per_trade=max_rebalance,
    )


class TestApproveTrade:
    def test_basic_approval(self):
        rm = _make_rm()
        result = rm.approve_trade(
            ticker="SPY",
            proposed_allocation_pct=0.005,
            current_positions={},
            price_data={},
        )
        assert result["approved"]
        assert result["approved_allocation_pct"] <= 0.01

    def test_capped_to_max_risk(self):
        rm = _make_rm()
        result = rm.approve_trade(
            ticker="SPY",
            proposed_allocation_pct=0.10,
            current_positions={},
            price_data={},
        )
        assert result["approved"]
        assert result["approved_allocation_pct"] <= rm.max_risk_per_trade

    def test_rejected_by_circuit_breaker(self):
        rm = _make_rm()
        result = rm.approve_trade(
            ticker="SPY",
            proposed_allocation_pct=0.005,
            current_positions={},
            price_data={},
            daily_pnl_pct=-0.035,
        )
        assert not result["approved"]
        assert result["circuit_level"] == 2

    def test_high_correlation_rejection(self):
        rm = _make_rm()
        prices_spy = pd.Series([100 + i * 0.5 for i in range(50)])
        prices_qqq = pd.Series([200 + i * 0.5 for i in range(50)])
        result = rm.approve_trade(
            ticker="QQQ",
            proposed_allocation_pct=0.005,
            current_positions={"SPY": 0.3},
            price_data={"SPY": prices_spy, "QQQ": prices_qqq},
        )
        assert result["correlation_warning"]

    def test_circuit_level1_halves_allocation(self):
        rm = _make_rm()
        result = rm.approve_trade(
            ticker="SPY",
            proposed_allocation_pct=0.008,
            current_positions={},
            price_data={},
            daily_pnl_pct=-0.02,
        )
        assert result["approved"]
        assert result["approved_allocation_pct"] <= 0.004 + 1e-9


class TestApproveRebalance:
    def test_allows_larger_allocation(self):
        rm = _make_rm()
        result = rm.approve_rebalance(
            ticker="SPY",
            proposed_allocation_pct=0.20,
        )
        assert result["approved"]
        assert result["approved_allocation_pct"] == pytest.approx(0.20)

    def test_capped_at_max_rebalance(self):
        rm = _make_rm(max_rebalance=0.25)
        result = rm.approve_rebalance(
            ticker="SPY",
            proposed_allocation_pct=0.40,
        )
        assert result["approved"]
        assert result["approved_allocation_pct"] == pytest.approx(0.25)

    def test_rejected_by_circuit_breaker_halt(self):
        rm = _make_rm()
        result = rm.approve_rebalance(
            ticker="SPY",
            proposed_allocation_pct=0.10,
            daily_pnl_pct=-0.035,
        )
        assert not result["approved"]


class TestApproveSell:
    def test_sell_approved_normally(self):
        rm = _make_rm()
        result = rm.approve_sell(ticker="SPY")
        assert result["approved"]

    def test_sell_blocked_on_halt(self):
        rm = _make_rm()
        result = rm.approve_sell(ticker="SPY", daily_pnl_pct=-0.035)
        assert not result["approved"]

    def test_sell_approved_at_level1(self):
        rm = _make_rm()
        result = rm.approve_sell(ticker="SPY", daily_pnl_pct=-0.02)
        assert result["approved"]


class TestUpdateEquity:
    def test_updates_both(self):
        rm = _make_rm(equity=100_000.0)
        rm.update_equity(120_000.0)
        assert rm.portfolio_equity == 120_000.0
        assert rm.circuit_breaker.peak_equity == 120_000.0

    def test_peak_not_lowered(self):
        rm = _make_rm(equity=100_000.0)
        rm.update_equity(120_000.0)
        rm.update_equity(110_000.0)
        assert rm.circuit_breaker.peak_equity == 120_000.0
        assert rm.portfolio_equity == 110_000.0
