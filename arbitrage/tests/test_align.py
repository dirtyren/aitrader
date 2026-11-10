import pandas as pd
import pytest

from arbitrage.align import align_pair, clock_skew_stats


def test_forward_fill_and_drop_stale():
    a = pd.DataFrame({"ts_utc_ms": [0, 10, 20], "bid": [100, 101, 102], "ask": [100.5, 101.5, 102.5]})
    b = pd.DataFrame({"ts_utc_ms": [0, 25], "bid": [100, 102], "ask": [100.5, 102.5]})
    aligned = align_pair({"a": a, "b": b}, grid_ms=10, staleness_ms=1000)
    # at ts=20, venue b's last quote is at 0 -> age 20ms < 1000 -> kept, forward-filled
    assert aligned.loc[20, "bid_b"] == 100.0


def test_stale_venue_row_dropped():
    a = pd.DataFrame({"ts_utc_ms": [0, 10, 20, 30], "bid": [100] * 4, "ask": [101] * 4})
    b = pd.DataFrame({"ts_utc_ms": [0], "bid": [100], "ask": [101]})
    aligned = align_pair({"a": a, "b": b}, grid_ms=10, staleness_ms=15)
    assert 20 not in aligned.index  # b stale (>15ms) at ts=20
    assert 10 in aligned.index


def test_skew_does_not_fabricate_spread():
    a = pd.DataFrame({"ts_utc_ms": [0, 10], "bid": [100, 100], "ask": [101, 101]})
    b = pd.DataFrame({"ts_utc_ms": [5, 15], "bid": [100, 100], "ask": [101, 101]})  # same prices, 5ms skewed
    aligned = align_pair({"a": a, "b": b}, grid_ms=10, staleness_ms=1000)
    # equal prices -> no price difference between venues (bid_b - bid_a == 0)
    assert (aligned["bid_b"] - aligned["bid_a"]).abs().max() == 0.0
    assert clock_skew_stats({"a": a, "b": b})["b"] == pytest.approx(5.0)
