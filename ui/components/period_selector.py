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


def render() -> tuple[datetime, datetime]:
    """Render the period selector. Returns the resolved (start, end) UTC."""
    import streamlit as st

    state = st.session_state
    state.setdefault("period_mode", "1M")  # default to 1 month
    state.setdefault("period_custom_start", (datetime.now(timezone.utc) - timedelta(days=30)).date())
    state.setdefault("period_custom_end", datetime.now(timezone.utc).date())

    cols = st.columns([6, 2, 2])
    options = PRESETS + ["Custom"]
    mode = cols[0].radio(
        "Period",
        options=options,
        index=options.index(state["period_mode"]) if state["period_mode"] in options else 0,
        horizontal=True,
        key="period_mode",
    )

    if mode == "Custom":
        start_d = cols[1].date_input("From", value=state["period_custom_start"], key="period_custom_start")
        end_d = cols[2].date_input("To", value=state["period_custom_end"], key="period_custom_end")
        start = datetime(start_d.year, start_d.month, start_d.day, 0, 0, tzinfo=timezone.utc)
        end = datetime(end_d.year, end_d.month, end_d.day, 23, 59, 59, tzinfo=timezone.utc)
        return start, end

    return resolve_preset(mode)
