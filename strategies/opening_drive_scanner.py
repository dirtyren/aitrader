"""Opening Drive scanner: universe, baselines, metrics, gates, ranking.

Screens the S&P 500 + Nasdaq-100 on the 09:30-10:00 opening range and
returns the day's ranked watchlist.

All metrics are self-normalized (symbol vs. its own trailing history, or a
ratio taken within one feed) because the market-data feed is IEX-only,
carrying roughly 2% of consolidated volume. Absolute cross-sectional
comparisons between symbols are invalid on this feed; ratios are not.

Split into pure functions plus a stateful holder so tests drive metrics,
gates, and ranking without any network access.
"""
from __future__ import annotations

import csv
import json
import logging
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from core.bar import Bar

logger = logging.getLogger(__name__)


def load_universe(path: str | Path) -> dict[str, str]:
    """Read `symbol,sector` CSV into a symbol -> sector mapping.

    Returns a dict rather than GapScanner's list because SectorExposureFilter
    needs the sector for every candidate. Symbols with no sector column get
    "UNKNOWN", which the sector cap then treats as its own bucket.
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Universe file not found: {p}")
    out: dict[str, str] = {}
    with p.open() as f:
        for i, row in enumerate(csv.reader(f)):
            if not row:
                continue
            symbol = row[0].strip().upper()
            if not symbol:
                continue
            if i == 0 and symbol == "SYMBOL":
                continue
            sector = row[1].strip() if len(row) > 1 and row[1].strip() else "UNKNOWN"
            out[symbol] = sector
    return out


@dataclass(frozen=True)
class OpeningDriveBaseline:
    """Per-symbol trailing statistics, refreshed post-close.

    avg_or_volume_20d is the mean IEX volume in the 09:30-10:00 window over
    the trailing 20 sessions. It is the denominator of rvol_or, and is the
    reason that metric is meaningful on a feed carrying ~2% of consolidated
    volume: the per-symbol IEX share cancels in the ratio.

    avg_daily_volume_20d is likewise IEX-denominated. Do not compare it to
    consolidated-volume thresholds (see the min_avg_daily_volume gate).

    There is deliberately no prev_close field: a skipped refresh would leave
    a stale prev_close that silently corrupts disp_atr and rs_atr. The loop
    fetches prev_close fresh at cut time.
    """
    atr_14d: float
    avg_or_volume_20d: float
    avg_daily_volume_20d: float
    computed_at: datetime


def load_baselines(path: str | Path) -> dict[str, OpeningDriveBaseline]:
    """Load the baselines JSON. A missing file yields {} so the caller can
    refresh; a malformed entry is skipped rather than failing the load."""
    p = Path(path)
    if not p.exists():
        return {}
    try:
        raw = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("OD_BASELINES_LOAD_FAILED path=%s error=%s", p, exc)
        return {}
    out: dict[str, OpeningDriveBaseline] = {}
    for sym, entry in (raw or {}).items():
        try:
            out[sym] = OpeningDriveBaseline(
                atr_14d=float(entry["atr_14d"]),
                avg_or_volume_20d=float(entry["avg_or_volume_20d"]),
                avg_daily_volume_20d=float(entry["avg_daily_volume_20d"]),
                computed_at=datetime.fromisoformat(
                    entry["computed_at"].replace("Z", "+00:00")
                ),
            )
        except (KeyError, TypeError, ValueError) as exc:
            logger.warning("OD_BASELINE_SKIP symbol=%s error=%s", sym, exc)
    return out


def save_baselines(baselines: dict[str, OpeningDriveBaseline],
                   path: str | Path) -> None:
    """Atomic-ish write via tmp + rename, so a crash mid-write cannot leave a
    truncated baselines file that the next session would load as garbage."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        sym: {
            "atr_14d": b.atr_14d,
            "avg_or_volume_20d": b.avg_or_volume_20d,
            "avg_daily_volume_20d": b.avg_daily_volume_20d,
            "computed_at": b.computed_at.astimezone(timezone.utc)
            .isoformat().replace("+00:00", "Z"),
        }
        for sym, b in baselines.items()
    }
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True))
    tmp.replace(p)


def baselines_age_p95_days(
    baselines: dict[str, OpeningDriveBaseline], now: datetime,
) -> float | None:
    """95th-percentile baseline age in days, or None when there are none.

    p95 rather than min(): a handful of permanently stale symbols (IEX names
    with no daily bars) must not block trading for the whole universe. This
    is the same lesson GapScanner records in its own docstring.
    """
    if not baselines:
        return None
    ages = sorted(
        (now - b.computed_at).total_seconds() / 86400.0
        for b in baselines.values()
    )
    return ages[min(int(len(ages) * 0.95), len(ages) - 1)]


def baselines_are_stale(
    baselines: dict[str, OpeningDriveBaseline], now: datetime,
    max_age_days: int,
) -> bool:
    """True when a refresh is recommended."""
    age = baselines_age_p95_days(baselines, now)
    return True if age is None else age > max_age_days


def baselines_too_old_to_trade(
    baselines: dict[str, OpeningDriveBaseline], now: datetime,
    max_age_days: int,
) -> bool:
    """True when baselines are so old that cutting would be unsafe.

    Hard fail-safe at 2x the recommended max age.
    """
    age = baselines_age_p95_days(baselines, now)
    return True if age is None else age > max_age_days * 2
