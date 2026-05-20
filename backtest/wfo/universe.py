"""Broker-asset scan with liquidity floor and disk-cached results."""
from __future__ import annotations
import hashlib
import json
import logging
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

import pandas as pd

logger = logging.getLogger(__name__)

_BARS_LOOKBACK_DAYS = 30          # fetch 30 calendar days, take last 20 trading bars


def _cache_key(asof_date: date, classes: list[str], floor: float,
               top_n: dict[str, int | None]) -> str:
    payload = json.dumps({"asof": asof_date.isoformat(),
                          "classes": sorted(classes),
                          "floor": floor,
                          "top_n": dict(sorted(top_n.items()))},
                         sort_keys=True, separators=(",", ":"))
    return hashlib.blake2b(payload.encode("utf-8"), digest_size=8).hexdigest()


def _read_cache(cache_path: Path) -> list[tuple[str, str]] | None:
    if not cache_path.exists():
        return None
    df = pd.read_parquet(cache_path)
    return list(zip(df["symbol"].tolist(), df["asset_class"].tolist()))


def _write_cache(cache_path: Path, rows: list[tuple[str, str]]) -> None:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame(rows, columns=["symbol", "asset_class"])
    tmp = cache_path.with_suffix(cache_path.suffix + ".tmp")
    df.to_parquet(tmp, index=False)
    tmp.replace(cache_path)


def _dollar_volume_20d(client, symbol: str, asset_class: str,
                       end: datetime) -> float:
    start = end - timedelta(days=_BARS_LOOKBACK_DAYS)
    if asset_class == "crypto":
        bars = client.get_crypto_bars(symbol, "1Day", start, end)
    else:
        bars = client.get_stock_bars(symbol, "1Day", start, end)
    if not bars:
        return 0.0
    last = bars[-20:]
    return float(sum(b["c"] * b["v"] for b in last) / max(len(last), 1))


def scan_alpaca_universe(
    client,
    *,
    classes: list[str],
    min_dollar_volume_20d: float,
    top_n_per_class: dict[str, int | None],
    cache_dir: Path | str,
    asof_date: date | None = None,
) -> list[tuple[str, str]]:
    """Return [(symbol, asset_class)] for active+tradable+liquid+top-N assets.

    Cached on disk per (asof_date, classes, floor, top_n) tuple.
    """
    asof_date = asof_date or date.today()
    cache_dir = Path(cache_dir)
    key = _cache_key(asof_date, classes, min_dollar_volume_20d, top_n_per_class)
    cache_path = cache_dir / f"{asof_date.isoformat()}_{key}.parquet"

    cached = _read_cache(cache_path)
    if cached is not None:
        return cached

    end_dt = datetime.combine(asof_date, time(0, tzinfo=timezone.utc))
    by_class: dict[str, list[tuple[str, float]]] = {c: [] for c in classes}

    assets = client.get_assets()
    for a in assets:
        if a.get("class") not in classes:
            continue
        if a.get("status") != "active" or not a.get("tradable"):
            continue
        symbol = a["symbol"]
        try:
            dv = _dollar_volume_20d(client, symbol, a["class"], end_dt)
        except Exception as exc:                                # noqa: BLE001
            logger.warning("UNIVERSE_BARS_FAILED symbol=%s err=%s", symbol, exc)
            continue
        if dv < min_dollar_volume_20d:
            continue
        by_class[a["class"]].append((symbol, dv))

    out: list[tuple[str, str]] = []
    for cls in classes:
        rows = sorted(by_class[cls], key=lambda r: r[1], reverse=True)
        cap = top_n_per_class.get(cls)
        if cap is not None:
            rows = rows[:cap]
        out.extend((sym, cls) for sym, _ in rows)

    _write_cache(cache_path, out)
    return out
