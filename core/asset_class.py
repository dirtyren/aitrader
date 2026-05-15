from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, time, timedelta

import pytz


@dataclass(frozen=True)
class AssetClassConfig:
    name: str
    timezone: str
    session_open_local: str          # "HH:MM"
    session_close_local: str
    opening_blackout_min: int
    bar_timeframe: str
    slippage_bps: float
    commission_per_share: float
    commission_bps: float


def _parse_hhmm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


def session_start_for(now_utc: datetime, cfg: AssetClassConfig) -> datetime:
    """Return the session-start timestamp (UTC) for the session that *contains* now_utc.

    If now is before today's open in the asset class's local timezone, returns yesterday's session start.
    """
    if now_utc.tzinfo is None:
        raise ValueError("now_utc must be timezone-aware")
    tz = pytz.timezone(cfg.timezone)
    now_local = now_utc.astimezone(tz)
    open_t = _parse_hhmm(cfg.session_open_local)
    today_open_local = tz.localize(datetime.combine(now_local.date(), open_t))
    if now_local < today_open_local:
        yday = (now_local - timedelta(days=1)).date()
        today_open_local = tz.localize(datetime.combine(yday, open_t))
    return today_open_local.astimezone(pytz.UTC)
