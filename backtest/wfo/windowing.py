"""IS/OOS rolling-window splitter with month/day-aware durations."""
from __future__ import annotations
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Union

from dateutil.relativedelta import relativedelta

Duration = Union[timedelta, relativedelta]

_DURATION_RE = re.compile(r"^(\d+)(d|mo)$")


def parse_duration(s: str) -> Duration:
    """Parse "6mo" → relativedelta(months=6); "180d" → timedelta(days=180)."""
    m = _DURATION_RE.match(s.strip()) if s else None
    if not m:
        raise ValueError(f"Invalid duration {s!r}; expected '<int>d' or '<int>mo'")
    n, unit = int(m.group(1)), m.group(2)
    if unit == "d":
        return timedelta(days=n)
    return relativedelta(months=n)


@dataclass(frozen=True)
class Walk:
    idx: int
    is_start: datetime
    is_end: datetime
    oos_start: datetime
    oos_end: datetime


def make_walks(start: datetime, end: datetime,
               in_sample: Duration, out_of_sample: Duration,
               step: Duration | None = None,
               anchored: bool = False) -> list[Walk]:
    """Generate rolling IS/OOS walks covering [start, end].

    Walks whose OOS would extend past `end` are dropped. `step` defaults to
    `out_of_sample` (classical Pardo: non-overlapping OOS).
    """
    if anchored:
        raise NotImplementedError("Anchored windowing reserved for v2")
    if step is None:
        step = out_of_sample

    walks: list[Walk] = []
    cursor = start
    idx = 0
    while True:
        is_start = cursor
        is_end = is_start + in_sample
        oos_start = is_end
        oos_end = oos_start + out_of_sample
        if oos_end > end:
            break
        walks.append(Walk(idx=idx, is_start=is_start, is_end=is_end,
                          oos_start=oos_start, oos_end=oos_end))
        cursor = cursor + step
        idx += 1
    return walks
