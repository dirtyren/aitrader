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
