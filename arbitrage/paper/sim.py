"""test_mode simulation loop: aligned books -> Signal -> PaperSink -> Ledger."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import AsyncIterator, Callable

from arbitrage.config import Config
from arbitrage.paper.ledger import Ledger
from arbitrage.paper.order_sink import LiveSink, OrderSink, PaperSink, Signal
from arbitrage.paper.resource_meter import ResourceMeter

# A book source yields dicts: {"ts_utc_ms": int, "pair": str,
#   "books": {venue: {"bid": float, "ask": float}}}
BookSource = AsyncIterator[dict]


class Simulator:
    def __init__(
        self,
        config: Config,
        sink: OrderSink | None,
        meter: ResourceMeter,
        ledger: Ledger,
        state_path: str | Path,
        source: BookSource | None = None,
        trigger_bps: float = 5.0,
        size_usd: float = 1000.0,
        state_interval_s: float = 1.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if sink is None:
            if not config.test_mode:
                raise ValueError("a sink must be supplied when test_mode is False")
            sink = PaperSink({k: e.taker_fee_bps for k, e in config.exchanges.items()})
        elif config.test_mode and isinstance(sink, LiveSink):
            raise ValueError("LiveSink is not allowed in test_mode")
        self.config = config
        self.sink = sink
        self.meter = meter
        self.ledger = ledger
        self.state_path = Path(state_path)
        self.source = source
        self.trigger_bps = trigger_bps
        self.size_usd = size_usd
        self.state_interval_s = state_interval_s
        self._clock = clock
        self._last_write: float | None = None
        self._last_spread_bps: float | None = None

    @staticmethod
    def best_spread(books: dict[str, dict]) -> tuple[str, str, float] | None:
        """Best (buy_venue, sell_venue, gross_bps) across ordered venue pairs."""
        best = None
        for buy, bb in books.items():
            for sell, sb in books.items():
                if buy == sell:
                    continue
                bps = (sb["bid"] - bb["ask"]) / bb["ask"] * 1e4
                if best is None or bps > best[2]:
                    best = (buy, sell, bps)
        return best

    def _on_book(self, item: dict) -> None:
        best = self.best_spread(item["books"])
        if best is None:
            return
        buy, sell, bps = best
        self._last_spread_bps = bps
        if bps > self.trigger_bps:
            sig = Signal(item["pair"], buy, sell, self.size_usd, bps, item["ts_utc_ms"])
            self.ledger.record(self.sink.submit(sig, item["books"]))

    def _write_state(self) -> None:
        state = {
            "test_mode": self.config.test_mode,
            "spread_bps": self._last_spread_bps,
            "pnl_usd": self.ledger.realized_pnl_usd(),
            "round_trips": self.ledger.round_trips(),
            "resources": self.meter.snapshot(),
        }
        tmp = self.state_path.with_name(self.state_path.name + ".tmp")
        tmp.write_text(json.dumps(state))
        os.replace(tmp, self.state_path)  # atomic on POSIX
        self._last_write = self._clock()

    def _maybe_write_state(self) -> None:
        if (self._last_write is None
                or self._clock() - self._last_write >= self.state_interval_s):
            self._write_state()

    async def run(self) -> None:
        if self.source is None:
            raise RuntimeError("no book source supplied")
        async for item in self.source:
            self._on_book(item)
            self._maybe_write_state()
        self._write_state()  # final flush
