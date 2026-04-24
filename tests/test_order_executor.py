"""Tests for broker.order_executor.OrderExecutor."""

import os
import math
import pytest

os.environ.setdefault("TRADING_ENV", "test")
os.environ.setdefault("ALPACA_API_KEY", "test")
os.environ.setdefault("ALPACA_SECRET_KEY", "test")

from unittest.mock import MagicMock
from strategies.base_strategy import SignalData
from risk.circuit_breakers import CircuitBreaker
from risk.manager import RiskManager
from broker.order_executor import OrderExecutor


def _make_executor(equity=100_000.0):
    cb = CircuitBreaker(peak_equity=equity)
    rm = RiskManager(portfolio_equity=equity, circuit_breaker=cb)
    mock_client = MagicMock()
    mock_client.get_quote.return_value = 100.0
    mock_client.submit_order.return_value = {"id": "test-order-123", "status": "accepted"}
    mock_client.get_positions.return_value = []
    return OrderExecutor(mock_client, rm), mock_client, rm


class TestExecuteSignal:
    def test_approved_order_submitted(self):
        ex, client, _ = _make_executor()
        signal = SignalData(
            regime="Bull", confidence=0.9, allocation_pct=0.005,
            leverage=1.0, stable=True, high_uncertainty=False,
        )
        result = ex.execute_signal("SPY", signal, 100_000.0)
        assert client.submit_order.called
        call_kwargs = client.submit_order.call_args
        assert call_kwargs[1]["side"] == "buy"

    def test_zero_shares_not_submitted(self):
        ex, client, _ = _make_executor()
        client.get_quote.return_value = 999_999.0
        signal = SignalData(
            regime="Bull", confidence=0.9, allocation_pct=0.001,
            leverage=1.0, stable=True, high_uncertainty=False,
        )
        result = ex.execute_signal("SPY", signal, 100_000.0)
        assert not client.submit_order.called
        assert result["shares"] == 0

    def test_rejected_by_risk_manager(self):
        ex, client, _ = _make_executor()
        signal = SignalData(
            regime="Crash", confidence=0.5, allocation_pct=0.005,
            leverage=1.0, stable=True, high_uncertainty=False,
        )
        result = ex.execute_signal("SPY", signal, 100_000.0, daily_pnl_pct=-0.035)
        assert not result.get("approved", True)
        assert not client.submit_order.called

    def test_share_quantity_correct(self):
        ex, client, _ = _make_executor()
        client.get_quote.return_value = 50.0
        signal = SignalData(
            regime="Bull", confidence=1.0, allocation_pct=0.01,
            leverage=1.0, stable=True, high_uncertainty=False,
        )
        ex.execute_signal("SPY", signal, 100_000.0)
        call_kwargs = client.submit_order.call_args[1]
        assert call_kwargs["qty"] == math.floor(0.01 * 100_000 / 50.0)


class TestRebalancePortfolio:
    def _make_portfolio(self):
        from core.portfolio import Portfolio, PortfolioAsset
        return Portfolio(
            assets=[
                PortfolioAsset("SPY", "S&P 500", "equity", 0.50, 0.10, 0.70),
                PortfolioAsset("QQQ", "Nasdaq", "equity", 0.50, 0.10, 0.70),
            ],
            rebalance_threshold=0.05,
            max_single_asset=0.70,
        )

    def test_buy_when_underweight(self):
        ex, client, _ = _make_executor()
        portfolio = self._make_portfolio()
        signal_map = {
            "SPY": SignalData("Bull", 0.9, 1.0, 1.0, False, True),
            "QQQ": SignalData("Bull", 0.9, 1.0, 1.0, False, True),
        }
        results = ex.rebalance_portfolio(
            portfolio=portfolio,
            current_positions={"SPY": 20_000.0, "QQQ": 20_000.0},
            signal_map=signal_map,
            current_equity=100_000.0,
        )
        buy_calls = [c for c in client.submit_order.call_args_list if c[1]["side"] == "buy"]
        assert len(buy_calls) > 0

    def test_sell_when_overweight(self):
        ex, client, _ = _make_executor()
        portfolio = self._make_portfolio()
        signal_map = {
            "SPY": SignalData("Bear", 0.9, 0.3, 1.0, False, True),
            "QQQ": SignalData("Bear", 0.9, 0.3, 1.0, False, True),
        }
        results = ex.rebalance_portfolio(
            portfolio=portfolio,
            current_positions={"SPY": 60_000.0, "QQQ": 60_000.0},
            signal_map=signal_map,
            current_equity=100_000.0,
        )
        sell_calls = [c for c in client.submit_order.call_args_list if c[1]["side"] == "sell"]
        assert len(sell_calls) > 0

    def test_skip_below_threshold(self):
        ex, client, _ = _make_executor()
        portfolio = self._make_portfolio()
        signal_map = {
            "SPY": SignalData("Bull", 0.9, 1.0, 1.0, False, True),
            "QQQ": SignalData("Bull", 0.9, 1.0, 1.0, False, True),
        }
        results = ex.rebalance_portfolio(
            portfolio=portfolio,
            current_positions={"SPY": 49_500.0, "QQQ": 49_500.0},
            signal_map=signal_map,
            current_equity=100_000.0,
        )
        assert not client.submit_order.called


class TestClosePosition:
    def test_close_existing_position(self):
        ex, client, _ = _make_executor()
        client.get_positions.return_value = [{"symbol": "SPY", "qty": "50"}]
        result = ex.close_position("SPY")
        client.submit_order.assert_called_once()
        call_kwargs = client.submit_order.call_args[1]
        assert call_kwargs["side"] == "sell"
        assert call_kwargs["qty"] == 50

    def test_close_no_position(self):
        ex, client, _ = _make_executor()
        client.get_positions.return_value = []
        result = ex.close_position("SPY")
        assert not client.submit_order.called
        assert result["shares"] == 0
