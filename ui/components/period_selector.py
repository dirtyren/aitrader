"""Time-period selector for the dashboard.

Pure resolution logic in `resolve_preset` (unit-testable).
A separate `render` function builds the Streamlit widgets and returns
the same `(start_utc, end_utc)` tuple — kept thin so the heavy logic
stays testable.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

PRESETS = ["1D", "1W", "15D", "1M", "6M", "1Y"]

_PRESET_DAYS = {
    "1D": 1, "1W": 7, "15D": 15,
    "1M": 30, "6M": 180, "1Y": 365,
}


def resolve_preset(preset: str, *, now: Optional[datetime] = None) -> tuple[datetime, datetime]:
    """Return (start_utc, end_utc) for a preset name. `now` defaults to now-UTC."""
    if preset not in _PRESET_DAYS:
        raise ValueError(f"Unknown preset: {preset!r}. Known: {PRESETS}")
    end = now or datetime.now(timezone.utc)
    start = end - timedelta(days=_PRESET_DAYS[preset])
    return start, end
