"""Binance public-archive bookTicker downloader (data.binance.vision).

NOTE: As verified against the live archive, bookTicker daily files exist only
for USD-M futures (market "um", path ``data/futures/um/...``), roughly
2023-05-16 .. 2024-03-30. Spot has no bookTicker archive.
"""
import hashlib
import os
import tempfile
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import requests

from arbitrage.connectors.base import VenueConnector

DEFAULT_BASE_URL = "https://data.binance.vision"
DEFAULT_CACHE_DIR = Path("runtime/bars_cache/arbitrage")

# market -> path prefix under /data
_MARKET_PATHS = {"um": "futures/um"}

# Real Binance header (update_id,best_bid_price,best_bid_qty,best_ask_price,
# best_ask_qty,transaction_time,event_time); canonical header is also accepted.
_BID_COLS = ("bid", "best_bid_price", "best bid price")
_ASK_COLS = ("ask", "best_ask_price", "best ask price")
_TS_COLS = ("ts_utc_ms", "event_time", "event time")


class ChecksumMismatchError(Exception):
    pass


def _pick(columns, candidates, what: str) -> str:
    for c in candidates:
        if c in columns:
            return c
    raise ValueError(f"bookTicker CSV has no {what} column; header={list(columns)}")


def parse_bookticker_csv(path) -> pd.DataFrame:
    """Parse a bookTicker CSV (path or file-like) to [ts_utc_ms, bid, ask], sorted ascending."""
    header = pd.read_csv(path, nrows=0).columns
    if hasattr(path, "seek"):
        path.seek(0)
    bid = _pick(header, _BID_COLS, "bid")
    ask = _pick(header, _ASK_COLS, "ask")
    ts = _pick(header, _TS_COLS, "timestamp")
    df = pd.read_csv(path, usecols=[ts, bid, ask])[[ts, bid, ask]]
    df.columns = ["ts_utc_ms", "bid", "ask"]
    df["ts_utc_ms"] = df["ts_utc_ms"].astype("int64")
    return df.sort_values("ts_utc_ms", kind="stable").reset_index(drop=True)


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def verify_checksum(path: Path, checksum_path: Path) -> None:
    """Raise ChecksumMismatchError unless sha256(path bytes) matches the .CHECKSUM file.

    Fails closed: a missing/empty checksum file is also a mismatch.
    """
    checksum_path = Path(checksum_path)
    if not checksum_path.is_file():
        raise ChecksumMismatchError(f"checksum file missing: {checksum_path}")
    tokens = checksum_path.read_text().split()
    if not tokens:
        raise ChecksumMismatchError(f"checksum file empty: {checksum_path}")
    expected = tokens[0].lower()
    actual = _sha256_file(Path(path))
    if actual != expected:
        raise ChecksumMismatchError(
            f"sha256 mismatch for {path}: expected {expected}, got {actual}"
        )


def _fetch(url: str, dest: Path) -> None:
    """Stream url to dest."""
    with requests.get(url, stream=True, timeout=60) as r:
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)


def download_and_cache(
    symbol: str,
    market: str,
    day: date,
    cache_dir: Path = DEFAULT_CACHE_DIR,
    base_url: str = DEFAULT_BASE_URL,
) -> Path:
    """Download one day's bookTicker zip, verify its .CHECKSUM, cache as parquet.

    Returns the parquet path. Cached days are not re-downloaded.
    """
    if market not in _MARKET_PATHS:
        raise ValueError(
            f"unsupported market {market!r}: Binance publishes bookTicker archives "
            f"only for {sorted(_MARKET_PATHS)} (spot has none)"
        )
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    out = cache_dir / f"binance_{market}_{symbol}_{day.isoformat()}.parquet"
    if out.is_file():
        return out

    name = f"{symbol}-bookTicker-{day.isoformat()}"
    url = f"{base_url}/data/{_MARKET_PATHS[market]}/daily/bookTicker/{symbol}/{name}.zip"
    with tempfile.TemporaryDirectory(dir=cache_dir) as tmp:
        zip_path = Path(tmp) / f"{name}.zip"
        cs_path = Path(tmp) / f"{name}.zip.CHECKSUM"
        _fetch(url, zip_path)
        _fetch(url + ".CHECKSUM", cs_path)
        verify_checksum(zip_path, cs_path)
        with zipfile.ZipFile(zip_path) as zf:
            with zf.open(f"{name}.csv") as f:
                df = parse_bookticker_csv(f)
        tmp_out = Path(tmp) / out.name
        df.to_parquet(tmp_out, index=False)
        os.replace(tmp_out, out)  # atomic publish (same filesystem as cache_dir)
    return out


class BinanceArchiveConnector(VenueConnector):
    """Loads cached/downloaded Binance bookTicker quotes.

    ``market`` is fixed per connector (constructor) so load_quotes keeps the
    VenueConnector signature.
    """

    def __init__(
        self,
        market: str = "um",
        cache_dir: Path = DEFAULT_CACHE_DIR,
        base_url: str = DEFAULT_BASE_URL,
    ):
        self.market = market
        self.cache_dir = Path(cache_dir)
        self.base_url = base_url

    def load_quotes(self, symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
        sym = symbol.replace("/", "").upper()
        start = start if start.tzinfo else start.replace(tzinfo=timezone.utc)
        end = end if end.tzinfo else end.replace(tzinfo=timezone.utc)
        start_ms = int(start.timestamp() * 1000)
        end_ms = int(end.timestamp() * 1000)
        frames = []
        day = start.astimezone(timezone.utc).date()
        last = end.astimezone(timezone.utc).date()
        while day <= last:
            p = download_and_cache(sym, self.market, day, self.cache_dir, self.base_url)
            frames.append(pd.read_parquet(p))
            day += timedelta(days=1)
        df = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(
            columns=["ts_utc_ms", "bid", "ask"]
        )
        df = df[(df["ts_utc_ms"] >= start_ms) & (df["ts_utc_ms"] <= end_ms)]
        return df.sort_values("ts_utc_ms", kind="stable").reset_index(drop=True)
