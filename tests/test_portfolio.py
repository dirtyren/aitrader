"""Tests for core.portfolio.Portfolio."""

import os
import pytest

os.environ.setdefault("TRADING_ENV", "test")

from core.portfolio import Portfolio, PortfolioAsset
from strategies.base_strategy import SignalData


def _make_portfolio():
    return Portfolio(
        assets=[
            PortfolioAsset("SPY", "S&P 500", "equity", 0.15, min_weight=0.05, max_weight=0.20),
            PortfolioAsset("QQQ", "Nasdaq", "equity", 0.15, min_weight=0.05, max_weight=0.20),
            PortfolioAsset("IWM", "Russell", "equity", 0.11, min_weight=0.00, max_weight=0.15),
            PortfolioAsset("XLF", "Financials", "equity", 0.07, min_weight=0.00, max_weight=0.10),
            PortfolioAsset("GLD", "Gold", "commodity", 0.07, min_weight=0.00, max_weight=0.10),
            PortfolioAsset("NVDA", "NVIDIA", "single_stock", 0.07, min_weight=0.00, max_weight=0.10),
            PortfolioAsset("AAPL", "Apple", "single_stock", 0.11, min_weight=0.00, max_weight=0.15),
            PortfolioAsset("TSLA", "Tesla", "single_stock", 0.07, min_weight=0.00, max_weight=0.10),
            PortfolioAsset("MSFT", "Microsoft", "single_stock", 0.11, min_weight=0.00, max_weight=0.15),
            PortfolioAsset("AMZN", "Amazon", "single_stock", 0.09, min_weight=0.00, max_weight=0.10),
        ],
        rebalance_threshold=0.05,
        max_single_asset=0.20,
    )


def _make_signal(alloc=1.0):
    return SignalData(
        regime="Bull", confidence=0.9, allocation_pct=alloc,
        leverage=1.0, stable=True, high_uncertainty=False,
    )


class TestWeightNormalization:
    def test_weights_sum_to_one(self):
        p = _make_portfolio()
        signal_map = {t: _make_signal(1.0) for t in p.tickers}
        weights = p.regime_adjusted_targets(signal_map)
        assert sum(weights.values()) == pytest.approx(1.0, abs=1e-6)

    def test_max_single_asset_enforced(self):
        p = _make_portfolio()
        signal_map = {
            "SPY": _make_signal(3.0),
            "QQQ": _make_signal(0.1),
            "IWM": _make_signal(0.1),
            "XLF": _make_signal(0.1),
            "GLD": _make_signal(0.1),
            "NVDA": _make_signal(0.1),
            "AAPL": _make_signal(0.1),
            "TSLA": _make_signal(0.1),
            "MSFT": _make_signal(0.1),
            "AMZN": _make_signal(0.1),
        }
        weights = p.regime_adjusted_targets(signal_map)
        for w in weights.values():
            assert w <= p.max_single_asset + 1e-6

    def test_all_zero_signals_still_valid(self):
        p = _make_portfolio()
        signal_map = {t: _make_signal(0.0) for t in p.tickers}
        weights = p.regime_adjusted_targets(signal_map)
        assert sum(weights.values()) == pytest.approx(1.0, abs=1e-6)
        for w in weights.values():
            assert w >= 0.0


class TestDrift:
    def test_no_drift_at_target(self):
        p = _make_portfolio()
        positions = {
            "SPY": 15_000, "QQQ": 15_000, "IWM": 11_000, "XLF": 7_000, "GLD": 7_000,
            "NVDA": 7_000, "AAPL": 11_000, "TSLA": 7_000, "MSFT": 11_000, "AMZN": 9_000,
        }
        drift = p.drift(positions, 100_000)
        for d in drift.values():
            assert abs(d) < 1e-6

    def test_drift_overweight(self):
        p = _make_portfolio()
        positions = {
            "SPY": 30_000, "QQQ": 10_000, "IWM": 10_000, "XLF": 5_000, "GLD": 5_000,
            "NVDA": 5_000, "AAPL": 10_000, "TSLA": 5_000, "MSFT": 10_000, "AMZN": 10_000,
        }
        drift = p.drift(positions, 100_000)
        assert drift["SPY"] > 0
        assert drift["QQQ"] < 0


class TestNeedsRebalance:
    def test_no_rebalance_at_target(self):
        p = _make_portfolio()
        positions = {
            "SPY": 15_000, "QQQ": 15_000, "IWM": 11_000, "XLF": 7_000, "GLD": 7_000,
            "NVDA": 7_000, "AAPL": 11_000, "TSLA": 7_000, "MSFT": 11_000, "AMZN": 9_000,
        }
        assert not p.needs_rebalance(positions, 100_000)

    def test_rebalance_on_large_drift(self):
        p = _make_portfolio()
        positions = {
            "SPY": 30_000, "QQQ": 10_000, "IWM": 10_000, "XLF": 5_000, "GLD": 5_000,
            "NVDA": 5_000, "AAPL": 10_000, "TSLA": 5_000, "MSFT": 10_000, "AMZN": 10_000,
        }
        assert p.needs_rebalance(positions, 100_000)


class TestPortfolioValidation:
    def test_weights_must_sum_to_one(self):
        with pytest.raises(ValueError, match="sum to 1.0"):
            Portfolio(assets=[
                PortfolioAsset("SPY", "test", "equity", 0.50),
                PortfolioAsset("QQQ", "test", "equity", 0.20),
            ])
