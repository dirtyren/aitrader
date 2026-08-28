from __future__ import annotations
import re
import time
from datetime import datetime, timedelta, timezone


_TF_RE = re.compile(r"^(\d+)(Min|Hour|Day)$")


def parse_timeframe_minutes(tf: str) -> int:
    m = _TF_RE.match(tf)
    if not m:
        raise ValueError(f"Unsupported timeframe: {tf!r}")
    n, unit = int(m.group(1)), m.group(2)
    if unit == "Min":
        return n
    if unit == "Hour":
        return n * 60
    return n * 1440  # Day — daily timeframes (used by main_daily.py)


def next_boundary(now: datetime, timeframe: str, grace_seconds: int = 5) -> datetime:
    """Return the next bar-close + grace timestamp strictly after `now`."""
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    minutes = parse_timeframe_minutes(timeframe)
    base = now.replace(second=0, microsecond=0)
    minute_of_hour = base.minute
    next_min = ((minute_of_hour // minutes) + 1) * minutes
    delta_minutes = next_min - minute_of_hour
    boundary = base + timedelta(minutes=delta_minutes)
    return boundary + timedelta(seconds=grace_seconds)


def sleep_until(target: datetime, *, sleeper=time.sleep, now_fn=lambda: datetime.now(timezone.utc)) -> None:
    """Sleep until target. `sleeper` and `now_fn` are seams for tests."""
    delta = (target - now_fn()).total_seconds()
    if delta > 0:
        sleeper(delta)
