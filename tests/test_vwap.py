import math
import numpy as np
from datetime import datetime, timezone, timedelta
from core.bar import Bar
from core.vwap import VWAPBands


def _make_bar(ts, price, volume=1000):
    return Bar(symbol="X", ts=ts,
               open=price, high=price + 0.5, low=price - 0.5,
               close=price, volume=volume)


def test_vwap_single_bar():
    v = VWAPBands(sigma=1.0)
    ts = datetime(2026, 5, 14, 13, 30, tzinfo=timezone.utc)
    v.add(_make_bar(ts, 100, volume=1000))
    assert v.vwap == 100.0
    assert v.upper == 100.0   # zero variance
    assert v.lower == 100.0


def test_vwap_matches_batch_calc():
    v = VWAPBands(sigma=1.0)
    base = datetime(2026, 5, 14, 13, 30, tzinfo=timezone.utc)
    prices = [100, 101, 99, 102, 98, 100.5]
    volumes = [1000, 2000, 1500, 800, 1200, 900]
    for i, (p, vol) in enumerate(zip(prices, volumes)):
        v.add(_make_bar(base + timedelta(minutes=5*i), p, volume=vol))

    # Batch reference using typical_price = (H+L+C)/3 with our synthetic OHLC
    typicals = np.array([(p + 0.5 + p - 0.5 + p) / 3.0 for p in prices])  # = prices
    vols = np.array(volumes, dtype=float)
    expected_vwap = np.average(typicals, weights=vols)
    expected_var = np.average((typicals - expected_vwap) ** 2, weights=vols)
    expected_sigma = math.sqrt(expected_var)

    assert math.isclose(v.vwap, expected_vwap, rel_tol=1e-9)
    assert math.isclose(v.upper, expected_vwap + expected_sigma, rel_tol=1e-9)
    assert math.isclose(v.lower, expected_vwap - expected_sigma, rel_tol=1e-9)


def test_vwap_reset():
    v = VWAPBands(sigma=1.0)
    ts = datetime(2026, 5, 14, 13, 30, tzinfo=timezone.utc)
    v.add(_make_bar(ts, 100))
    v.reset()
    assert v.bar_count == 0
    assert math.isnan(v.vwap)
