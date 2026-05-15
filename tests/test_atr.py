from datetime import datetime, timezone, timedelta
from core.bar import Bar
from core.atr import atr


def _b(ts, o, h, l, c):
    return Bar(symbol="X", ts=ts, open=o, high=h, low=l, close=c, volume=1000)


def test_atr_handles_short_window():
    base = datetime(2026, 5, 14, 13, 30, tzinfo=timezone.utc)
    bars = [_b(base + timedelta(minutes=5*i), 100, 101, 99, 100) for i in range(3)]
    # window 14 but we only have 3 bars → return mean of available true ranges = 2.0
    assert abs(atr(bars, 14) - 2.0) < 1e-9


def test_atr_full_window():
    base = datetime(2026, 5, 14, 13, 30, tzinfo=timezone.utc)
    bars = [_b(base + timedelta(minutes=5*i), 100, 100 + 0.5*i, 100 - 0.5*i, 100) for i in range(15)]
    val = atr(bars, 14)
    assert val > 0
    assert val < 20
