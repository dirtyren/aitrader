"""Reads a venue's recorder output: ``<data_dir>/<venue>/<SYM>.parquet``."""
from datetime import datetime
from pathlib import Path

import pandas as pd

from arbitrage.connectors.base import VenueConnector


class RecorderParquetConnector(VenueConnector):
    def __init__(self, data_dir, venue: str):
        self.data_dir = Path(data_dir)
        self.venue = venue

    def load_quotes(self, symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
        path = self.data_dir / self.venue / (symbol.replace("/", "_") + ".parquet")
        if not path.exists():
            return pd.DataFrame({"ts_utc_ms": [], "bid": [], "ask": []}).astype(
                {"ts_utc_ms": "int64", "bid": "float64", "ask": "float64"}
            )
        df = pd.read_parquet(path, columns=["ts_utc_ms", "bid", "ask"])
        df = df.dropna(subset=["ts_utc_ms"])  # rows without an exchange timestamp are unusable
        df["ts_utc_ms"] = df["ts_utc_ms"].astype("int64")
        lo, hi = int(start.timestamp() * 1000), int(end.timestamp() * 1000)
        df = df[(df["ts_utc_ms"] >= lo) & (df["ts_utc_ms"] <= hi)]
        df = df.sort_values("ts_utc_ms", kind="stable")
        return df.reset_index(drop=True)[["ts_utc_ms", "bid", "ask"]]
