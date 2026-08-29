"""Build the Opening Drive baselines file. Runs post-close (16:10 NY).

Computes, per symbol:
  - atr_14d               daily ATR(14)
  - avg_or_volume_20d     mean IEX volume in the 09:30-10:00 window over the
                          trailing N sessions -- the denominator of rvol_or
  - avg_daily_volume_20d  mean IEX daily volume (IEX-denominated!)

Cost: one bulk 1Day request plus one bulk 1Min request per trailing session
(21 calls at the default 20-session lookback), each covering all ~516
symbols. Runs once daily.

Usage:
    python scripts/build_opening_drive_baselines.py \
        --config config/settings_opening_drive_equity.yaml
"""
from __future__ import annotations

import argparse
import logging
from datetime import date, datetime, timedelta, timezone

import pytz
import yaml

from core.atr import atr as compute_atr
from core.bar import Bar
from strategies.opening_drive_scanner import (
    OpeningDriveBaseline, load_universe, save_baselines,
)

logger = logging.getLogger(__name__)

_NY_TZ = pytz.timezone("America/New_York")
BENCHMARK = "SPY"


def session_dates(spy_daily: list[Bar], now: datetime, n: int) -> list[date]:
    """The last `n` trading dates strictly before today, from SPY's bars.

    SPY trades every open session, so the dates present in its daily bars ARE
    the trading calendar for this window -- no market-calendar dependency and
    no chance of getting holidays wrong.

    Today is excluded: the current session is in progress, and its opening
    range is exactly what these baselines will be compared against.
    """
    today = now.astimezone(_NY_TZ).date()
    dates = sorted({
        b.ts.astimezone(_NY_TZ).date() for b in spy_daily
        if b.ts.astimezone(_NY_TZ).date() < today
    })
    return dates[-n:] if n > 0 else []


def _or_window_utc(day: date, or_minutes: int) -> tuple[datetime, datetime]:
    start = _NY_TZ.localize(
        datetime.combine(day, datetime.min.time().replace(hour=9, minute=30))
    ).astimezone(timezone.utc)
    return start, start + timedelta(minutes=or_minutes)


def build_baselines(
    data,
    symbols: list[str],
    now: datetime,
    or_minutes: int = 30,
    lookback_sessions: int = 20,
    atr_window: int = 14,
) -> dict[str, OpeningDriveBaseline]:
    """Compute baselines for every symbol with sufficient data.

    Symbols lacking daily bars, ATR history, or any opening-range volume are
    omitted rather than written with zeros -- compute_or_metrics treats a
    zero avg_or_volume_20d as unusable anyway, and omitting makes the gap
    visible in the baseline count.
    """
    daily = data.get_bars_multi(
        symbols, "equity", "1Day", now - timedelta(days=60), now,
    )
    dates = session_dates(daily.get(BENCHMARK) or [], now, lookback_sessions)
    if not dates:
        logger.error("OD_BASELINE_NO_SESSIONS benchmark=%s — cannot build",
                     BENCHMARK)
        return {}

    or_totals: dict[str, list[float]] = {}
    for day in dates:
        start, end = _or_window_utc(day, or_minutes)
        try:
            minute_bars = data.get_bars_multi(
                symbols, "equity", "1Min", start, end,
            )
        except Exception as exc:
            logger.warning("OD_BASELINE_SESSION_FETCH_FAILED day=%s error=%s",
                           day, exc)
            continue
        for sym, bars in minute_bars.items():
            total = sum(b.volume for b in bars)
            if total > 0:
                or_totals.setdefault(sym, []).append(total)

    out: dict[str, OpeningDriveBaseline] = {}
    for sym in symbols:
        bars = daily.get(sym) or []
        if len(bars) < atr_window + 1:
            logger.debug("OD_BASELINE_SKIP symbol=%s reason=insufficient_daily",
                         sym)
            continue
        atr_14d = compute_atr(bars[-(atr_window + 1):], atr_window)
        if atr_14d <= 0:
            logger.debug("OD_BASELINE_SKIP symbol=%s reason=non_positive_atr",
                         sym)
            continue
        recent = bars[-lookback_sessions:]
        adv = sum(b.volume for b in recent) / len(recent)
        totals = or_totals.get(sym) or []
        if not totals:
            logger.debug("OD_BASELINE_SKIP symbol=%s reason=no_or_volume", sym)
            continue
        out[sym] = OpeningDriveBaseline(
            atr_14d=atr_14d,
            avg_or_volume_20d=sum(totals) / len(totals),
            avg_daily_volume_20d=adv,
            computed_at=now,
        )

    logger.info("OD_BASELINES_BUILT symbols=%d of %d requested",
                len(out), len(symbols))
    return out


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config/settings_opening_drive_equity.yaml")
    args = ap.parse_args()

    with open(args.config) as f:
        cfg = yaml.safe_load(f)
    scan = cfg["scanner"]

    from broker.alpaca_client import AlpacaClient
    from broker.alpaca_data import AlpacaData

    universe = load_universe(scan["universe_file"])
    symbols = sorted(universe) + [BENCHMARK]

    client = AlpacaClient(asset_class="equity")
    data = AlpacaData(client, cache_dir="runtime/bars_cache")

    baselines = build_baselines(
        data, symbols, datetime.now(timezone.utc),
        or_minutes=scan.get("or_minutes", 30),
        lookback_sessions=scan.get("lookback_sessions", 20),
    )
    if not baselines:
        logger.error("OD_BASELINES_EMPTY — not overwriting existing file")
        return 1
    save_baselines(baselines, scan["baselines_path"])
    logger.info("OD_BASELINES_SAVED path=%s n=%d",
                scan["baselines_path"], len(baselines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
