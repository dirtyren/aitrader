from __future__ import annotations
import logging
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import pandas as pd

from broker.alpaca_client import AlpacaClient
from broker.symbol import normalize_for_api
from core.bar import Bar

logger = logging.getLogger(__name__)


def _parse_ts(raw: str) -> datetime:
    return datetime.fromisoformat(raw.replace("Z", "+00:00"))


def _bars_from_raw(raw: list[dict], symbol: str) -> list[Bar]:
    out: list[Bar] = []
    for r in raw:
        try:
            out.append(Bar(
                symbol=symbol,
                ts=_parse_ts(r["t"]),
                open=float(r["o"]),
                high=float(r["h"]),
                low=float(r["l"]),
                close=float(r["c"]),
                volume=float(r["v"]),
            ))
        except (KeyError, ValueError) as exc:
            logger.warning("Skipping malformed bar for %s: %s (%s)", symbol, r, exc)
    return out


class AlpacaData:
    """Wrapper over AlpacaClient bar endpoints with on-disk parquet cache."""

    def __init__(self, client: AlpacaClient, cache_dir: str = "runtime/bars_cache"):
        self.client = client
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _cache_path(self, symbol: str, timeframe: str, start: datetime, end: datetime) -> Path:
        safe_symbol = symbol.replace("/", "-")
        key = f"{safe_symbol}_{timeframe}_{start.isoformat()}_{end.isoformat()}.parquet"
        return self.cache_dir / key

    def _read_cache(self, path: Path, symbol: str) -> list[Bar] | None:
        if not path.exists():
            return None
        try:
            df = pd.read_parquet(path)
        except Exception as exc:
            logger.warning("Cache read failed for %s: %s — refetching", path, exc)
            return None
        return [
            Bar(symbol=symbol, ts=row.ts.to_pydatetime(),
                open=row.open, high=row.high, low=row.low,
                close=row.close, volume=row.volume)
            for row in df.itertuples(index=False)
        ]

    def _write_cache(self, path: Path, bars: list[Bar]) -> None:
        if not bars:
            return
        df = pd.DataFrame([{
            "ts": b.ts, "open": b.open, "high": b.high, "low": b.low,
            "close": b.close, "volume": b.volume,
        } for b in bars])
        tmp = path.with_suffix(".parquet.tmp")
        df.to_parquet(tmp, index=False)
        os.replace(tmp, path)

    def get_bars(
        self,
        symbol: str,
        asset_class: str,
        timeframe: str,
        start: datetime,
        end: datetime,
        use_cache: bool = True,
    ) -> list[Bar]:
        api_symbol = normalize_for_api(symbol, asset_class)
        cache_path = self._cache_path(symbol, timeframe, start, end)

        if use_cache:
            cached = self._read_cache(cache_path, symbol)
            if cached is not None:
                return cached

        if asset_class == "equity":
            raw = self.client.get_stock_bars(api_symbol, timeframe, start, end)
        elif asset_class == "crypto":
            raw = self.client.get_crypto_bars(api_symbol, timeframe, start, end)
        else:
            raise ValueError(f"Unknown asset_class: {asset_class}")

        bars = _bars_from_raw(raw, symbol)
        if use_cache:
            self._write_cache(cache_path, bars)
        return bars
