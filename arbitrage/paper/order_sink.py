"""Order sink abstraction: paper fills are simulated, live trading is stubbed."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class Signal:
    pair: str
    venue_buy: str
    venue_sell: str
    size_usd: float
    gross_bps: float
    ts_utc_ms: int


@dataclass(frozen=True)
class Fill:
    signal: Signal
    buy_price: float
    sell_price: float
    fee_usd: float
    pnl_usd: float
    ts_utc_ms: int


class OrderSink(Protocol):
    def submit(self, signal: Signal, book: dict) -> Fill: ...


class PaperSink:
    """Simulated fills. `taker_fee_bps` maps venue -> taker fee (bps per leg)."""

    def __init__(self, taker_fee_bps: dict[str, float], latency_ms: int = 0,
                 slippage_bps: float = 0.0):
        self.taker_fee_bps = taker_fee_bps
        self.latency_ms = latency_ms
        self.slippage_bps = slippage_bps

    def submit(self, signal: Signal, book: dict) -> Fill:
        from arbitrage.paper.fill_engine import simulate_fill  # avoid import cycle

        # simulate_fill charges fee_bps on both legs; the mean of the two venues'
        # taker fees makes the total equal buy-venue fee + sell-venue fee.
        fee_bps = (self.taker_fee_bps[signal.venue_buy]
                   + self.taker_fee_bps[signal.venue_sell]) / 2
        return simulate_fill(signal, book, self.latency_ms, self.slippage_bps, fee_bps)


class LiveSink:
    """Live trading is out of scope: construction always fails."""

    def __init__(self, *args, **kwargs):
        raise NotImplementedError("live trading is not implemented")

    def submit(self, signal: Signal, book: dict) -> Fill:  # pragma: no cover
        raise NotImplementedError("live trading is not implemented")
