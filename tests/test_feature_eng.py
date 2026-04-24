"""Tests for core.feature_eng.build_features."""

import os
import pytest
import numpy as np
import pandas as pd

os.environ.setdefault("TRADING_ENV", "test")

from core.feature_eng import build_features


class TestBuildFeatures:
    def test_output_columns(self):
        prices = pd.Series(np.cumsum(np.random.randn(100)) + 100)
        df = build_features(prices)
        assert list(df.columns) == ["log_return", "volatility", "volume_change"]

    def test_drops_leading_nans(self):
        prices = pd.Series(np.cumsum(np.random.randn(100)) + 100)
        df = build_features(prices, window=20)
        assert not df.isna().any().any()
        assert len(df) <= 100 - 20

    def test_volume_change_populated(self):
        prices = pd.Series(np.cumsum(np.random.randn(100)) + 100)
        volume = pd.Series(np.random.randint(1000, 5000, 100).astype(float))
        df = build_features(prices, volume_series=volume)
        assert (df["volume_change"] != 0.0).any()

    def test_volume_change_zero_when_absent(self):
        prices = pd.Series(np.cumsum(np.random.randn(100)) + 100)
        df = build_features(prices)
        assert (df["volume_change"] == 0.0).all()

    def test_log_return_values(self):
        rng = np.random.default_rng(42)
        prices = pd.Series(np.cumsum(rng.normal(0.5, 1, 50)) + 100)
        df = build_features(prices, window=5)
        assert len(df) > 0
        first_idx = df.index[0]
        expected = np.log(prices.iloc[first_idx] / prices.iloc[first_idx - 1])
        assert df["log_return"].iloc[0] == pytest.approx(expected, abs=1e-6)
