#!/usr/bin/env python3
"""Fetch and cache historical bars for a universe CSV across one or more timeframes.

Resumable: skips any (symbol, timeframe) whose parquet cache file already exists
for the requested window. Uses AlpacaData (which handles parquet cache + 429
backoff via AlpacaClient).

Usage:
    python scripts/cache_bars_universe.py \
        --universe config/universe_russell_1000.csv \
        --timeframes 1Hour \
        --start 2024-01-01 --end 2026-08-31 \
        --asset-class equity

Exit code: 0 if all symbols succeeded, 1 if any failed (still leaves the cache
populated for those that did succeed — re-running picks up where it left off).
"""
from __future__ import annotations

import argparse
import csv
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# Make repo root importable when invoked directly.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from broker.alpaca_client import AlpacaClient
from broker.alpaca_data import AlpacaData

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("cache_bars_universe")


def _load_universe(path: Path) -> list[str]:
    syms: list[str] = []
    with path.open() as f:
        reader = csv.reader(f)
        header = next(reader, None)
        if header and header[0].strip().lower() != "symbol":
            # No header — first row is data.
            syms.append(header[0].strip().upper())
        for row in reader:
            if not row or not row[0].strip():
                continue
            syms.append(row[0].strip().upper())
    return sorted(set(syms))


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--universe", required=True, type=Path,
                   help="CSV with one ticker per row (header 'symbol' optional).")
    p.add_argument("--timeframes", required=True, nargs="+",
                   help="One or more Alpaca timeframes, e.g. 1Hour 5Min.")
    p.add_argument("--start", required=True,
                   help="ISO date YYYY-MM-DD (inclusive).")
    p.add_argument("--end", required=True,
                   help="ISO date YYYY-MM-DD (inclusive).")
    p.add_argument("--asset-class", default="equity",
                   choices=("equity", "crypto"))
    p.add_argument("--cache-dir", default="runtime/bars_cache")
    p.add_argument("--limit", type=int, default=None,
                   help="Cap symbols processed (debug helper).")
    args = p.parse_args()

    syms = _load_universe(args.universe)
    if args.limit:
        syms = syms[: args.limit]
    log.info("UNIVERSE_LOADED count=%d path=%s", len(syms), args.universe)

    start = datetime.fromisoformat(args.start).replace(tzinfo=timezone.utc)
    end = datetime.fromisoformat(args.end).replace(tzinfo=timezone.utc)

    client = AlpacaClient(asset_class=args.asset_class)
    data = AlpacaData(client=client, cache_dir=args.cache_dir)

    total = len(syms) * len(args.timeframes)
    failed: list[tuple[str, str, str]] = []
    skipped = 0
    fetched = 0

    for tf in args.timeframes:
        for i, sym in enumerate(syms, 1):
            cache_path = data._cache_path(sym, tf, start, end)
            if cache_path.exists():
                skipped += 1
                if i % 50 == 0:
                    log.info("PROGRESS tf=%s %d/%d (skipped cached=%d fetched=%d failed=%d)",
                             tf, i, len(syms), skipped, fetched, len(failed))
                continue
            try:
                bars = data.get_bars(
                    symbol=sym, asset_class=args.asset_class,
                    timeframe=tf, start=start, end=end, use_cache=True,
                )
                if not bars:
                    log.warning("EMPTY_RESPONSE sym=%s tf=%s — caching empty", sym, tf)
                fetched += 1
            except Exception as exc:  # noqa: BLE001 — log + continue
                log.error("FETCH_FAILED sym=%s tf=%s err=%s", sym, tf, exc)
                failed.append((sym, tf, str(exc)))
                continue
            if i % 25 == 0:
                log.info("PROGRESS tf=%s %d/%d (skipped=%d fetched=%d failed=%d)",
                         tf, i, len(syms), skipped, fetched, len(failed))

    log.info(
        "DONE total=%d skipped_cached=%d fetched=%d failed=%d",
        total, skipped, fetched, len(failed),
    )
    if failed:
        log.warning("FAILURES (first 20):")
        for sym, tf, err in failed[:20]:
            log.warning("  %s %s: %s", sym, tf, err)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
