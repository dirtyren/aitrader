from datetime import datetime, timezone
from scheduler.bar_clock import next_boundary, parse_timeframe_minutes


def test_parse_timeframe_minutes():
    assert parse_timeframe_minutes("5Min") == 5
    assert parse_timeframe_minutes("15Min") == 15
    assert parse_timeframe_minutes("1Hour") == 60


def test_next_boundary_5min():
    now = datetime(2026, 5, 14, 13, 32, 12, tzinfo=timezone.utc)
    nb = next_boundary(now, "5Min", grace_seconds=5)
    assert nb == datetime(2026, 5, 14, 13, 35, 5, tzinfo=timezone.utc)


def test_next_boundary_exactly_on_boundary_advances():
    now = datetime(2026, 5, 14, 13, 35, 0, tzinfo=timezone.utc)
    nb = next_boundary(now, "5Min", grace_seconds=5)
    assert nb == datetime(2026, 5, 14, 13, 40, 5, tzinfo=timezone.utc)


def test_next_boundary_15min():
    now = datetime(2026, 5, 14, 13, 22, tzinfo=timezone.utc)
    assert next_boundary(now, "15Min", grace_seconds=0) == datetime(2026, 5, 14, 13, 30, 0, tzinfo=timezone.utc)
