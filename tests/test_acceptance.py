from datetime import datetime, timezone, timedelta
from core.bar import Bar
from core.acceptance import accepted_above, accepted_below


def _b(ts, c, h=None, l=None):
    return Bar(symbol="X", ts=ts, open=c, high=h or c + 0.5,
               low=l or c - 0.5, close=c, volume=1000)


def test_two_closes_above_with_distance():
    base = datetime(2026, 5, 14, 13, 30, tzinfo=timezone.utc)
    bars = [_b(base + timedelta(minutes=5*i), 101 + i * 0.5) for i in range(2)]
    # ATR = 0.5 (synthetic), level = 100
    assert accepted_above(bars, level=100.0, n=2, min_distance_atr=0.25, atr=0.5)


def test_one_close_above_not_accepted():
    base = datetime(2026, 5, 14, 13, 30, tzinfo=timezone.utc)
    bars = [_b(base, 99.5), _b(base + timedelta(minutes=5), 100.2)]
    # only the second bar is above
    assert not accepted_above(bars, level=100.0, n=2, min_distance_atr=0.0, atr=0.5)


def test_distance_threshold_rejects():
    base = datetime(2026, 5, 14, 13, 30, tzinfo=timezone.utc)
    bars = [_b(base, 100.05), _b(base + timedelta(minutes=5), 100.06)]
    # both close above 100 but distance is 0.06, below 0.25 × ATR(0.5)=0.125
    assert not accepted_above(bars, level=100.0, n=2, min_distance_atr=0.25, atr=0.5)


def test_below_symmetric():
    base = datetime(2026, 5, 14, 13, 30, tzinfo=timezone.utc)
    bars = [_b(base, 99.0), _b(base + timedelta(minutes=5), 98.5)]
    assert accepted_below(bars, level=100.0, n=2, min_distance_atr=0.25, atr=0.5)
