"""Live L1 recorder: ccxt.pro watch_order_book -> per-venue/pair parquet files."""
import argparse
import asyncio
import logging
import signal
import time
from pathlib import Path
from typing import Any, Callable, Optional

import pandas as pd

from arbitrage.config import Config, load_config
from arbitrage.connectors.base import Quote
from arbitrage.connectors.ccxt_venue import CcxtVenue, make_exchange

logger = logging.getLogger(__name__)


class Recorder:
    def __init__(
        self,
        config: Config,
        out_dir: Path,
        exchange_factory: Optional[Callable[[str], Any]] = None,
        max_updates_per_stream: Optional[int] = None,
        flush_rows: int = 200,
        flush_interval_s: float = 5.0,
        retry_delay_s: float = 1.0,
    ):
        self.config = config
        self.out_dir = Path(out_dir)
        self._factory = exchange_factory or make_exchange
        self._max_updates = max_updates_per_stream
        self._flush_rows = flush_rows
        self._flush_interval_s = flush_interval_s
        self._retry_delay_s = retry_delay_s
        self._buffers: dict[tuple[str, str], list[Quote]] = {}
        self._rows: dict[str, int] = {v: 0 for v in config.exchanges}
        self._last_recv: dict[str, float] = {}
        self._tasks: list[asyncio.Task] = []

    @staticmethod
    def _path(out_dir: Path, venue: str, symbol: str) -> Path:
        return out_dir / venue / (symbol.replace("/", "_") + ".parquet")

    def _flush(self, key: tuple[str, str]) -> None:
        rows = self._buffers.get(key)
        if not rows:
            return
        venue, symbol = key
        path = self._path(self.out_dir, venue, symbol)
        path.parent.mkdir(parents=True, exist_ok=True)
        new = pd.DataFrame(
            {
                "ts_utc_ms": pd.array([q.ts_utc_ms for q in rows], dtype="Int64"),
                "recv_ts_utc_ms": pd.array([q.recv_ts_utc_ms for q in rows], dtype="Int64"),
                "bid": [q.bid for q in rows],
                "ask": [q.ask for q in rows],
                "bid_sz": pd.array([q.bid_sz for q in rows], dtype="Float64"),
                "ask_sz": pd.array([q.ask_sz for q in rows], dtype="Float64"),
            }
        )
        if path.exists():
            new = pd.concat([pd.read_parquet(path), new], ignore_index=True)
        new.to_parquet(path, index=False)
        self._buffers[key] = []

    def _record(self, q: Quote) -> None:
        key = (q.venue, q.symbol)
        self._buffers.setdefault(key, []).append(q)
        self._rows[q.venue] = self._rows.get(q.venue, 0) + 1
        self._last_recv[q.venue] = time.time()

    async def _stream(self, venue: CcxtVenue, pair: str) -> None:
        key = (venue.venue, CcxtVenue.normalize(pair))
        n = 0
        last_flush = time.monotonic()
        try:
            while self._max_updates is None or n < self._max_updates:
                try:
                    q = await venue.watch_l1(pair)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # network drop etc.: log and retry
                    logger.warning("%s %s watch failed: %r", venue.venue, pair, exc)
                    await asyncio.sleep(self._retry_delay_s)
                    continue
                n += 1  # count every update event, empty or not
                if q is None:
                    continue
                self._record(q)
                if (
                    len(self._buffers[key]) >= self._flush_rows
                    or time.monotonic() - last_flush >= self._flush_interval_s
                ):
                    self._flush(key)
                    last_flush = time.monotonic()
        finally:
            self._flush(key)

    async def run(self) -> None:
        """Record until stop() is called (or max_updates_per_stream reached on every stream)."""
        venues = {
            name: CcxtVenue(name, self._factory(cfg.name))
            for name, cfg in self.config.exchanges.items()
        }
        self._tasks = [
            asyncio.ensure_future(self._stream(v, pair))
            for v in venues.values()
            for pair in self.config.pairs
        ]
        try:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        finally:
            for v in venues.values():
                try:
                    await v.close()
                except Exception as exc:
                    logger.warning("%s close failed: %r", v.venue, exc)

    def stop(self) -> None:
        for t in self._tasks:
            t.cancel()

    def live_state(self) -> dict:
        """Per-venue {'rows': recorded row count, 'last_quote_age_ms': ms since last quote or None}."""
        now = time.time()
        return {
            v: {
                "rows": self._rows.get(v, 0),
                "last_quote_age_ms": (
                    int((now - self._last_recv[v]) * 1000) if v in self._last_recv else None
                ),
            }
            for v in self.config.exchanges
        }


async def _amain(args: argparse.Namespace) -> None:
    rec = Recorder(load_config(args.config), Path(args.out_dir))
    loop = asyncio.get_running_loop()
    try:
        loop.add_signal_handler(signal.SIGINT, rec.stop)
        loop.add_signal_handler(signal.SIGTERM, rec.stop)
    except NotImplementedError:  # e.g. Windows event loops
        pass
    await rec.run()  # run() flushes buffers and closes exchanges on exit


def main(argv: Optional[list[str]] = None) -> None:
    p = argparse.ArgumentParser(prog="python -m arbitrage.recorder")
    p.add_argument("--config", default="arbitrage/config.yaml")
    p.add_argument("--out-dir", default="arbitrage/data")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_amain(args))


if __name__ == "__main__":
    main()
