from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from core.bar import Bar
from state.position_book import OpenPosition, PositionBook


@dataclass
class PendingOrder:
    symbol: str
    side: str                    # "buy" | "sell"
    qty: float
    order_type: str              # "limit" | "market" | "stop"
    limit_price: Optional[float]
    stop_price: float
    target_price: float
    asset_class: str
    setup: str
    ts: datetime


@dataclass
class FillEvent:
    symbol: str
    fill_price: float
    qty: float
    side: str


class SimulatedFillEngine:
    """Per-bar fill simulator for backtests.

    process_bar walks pending orders for the symbol and resolves each one
    against the bar's OHLC. Filled orders are added to the PositionBook
    with the same OpenPosition shape produced by OrderExecutor in live
    trading (initial_stop_px set so downstream R-calc matches).
    """

    def __init__(self, slippage_bps_by_class: dict[str, float]):
        self.slippage_bps_by_class = slippage_bps_by_class
        self.pending: list[PendingOrder] = []

    def submit(self, order: PendingOrder) -> None:
        self.pending.append(order)

    def _slippage(self, asset_class: str) -> float:
        return self.slippage_bps_by_class.get(asset_class, 0.0) / 10_000.0

    def process_bar(self, symbol: str, bar: Bar, book: PositionBook) -> list[FillEvent]:
        fills: list[FillEvent] = []
        remaining: list[PendingOrder] = []
        for o in self.pending:
            if o.symbol != symbol:
                remaining.append(o)
                continue

            fill_px = self._resolve_fill(o, bar)
            if fill_px is None:
                remaining.append(o)
                continue

            side_pos = "long" if o.side == "buy" else "short"
            if book.get(o.symbol) is None:
                book.add(OpenPosition(
                    symbol=o.symbol, setup=o.setup, side=side_pos,
                    qty=o.qty, entry_px=fill_px,
                    stop_px=o.stop_price, target_px=o.target_price,
                    opened_at=bar.ts, order_id=f"sim-{bar.ts.isoformat()}",
                    initial_stop_px=o.stop_price,
                ))
            fills.append(FillEvent(symbol=o.symbol, fill_price=fill_px,
                                   qty=o.qty, side=o.side))

        self.pending = remaining
        return fills

    def _resolve_fill(self, o: PendingOrder, bar: Bar) -> Optional[float]:
        slip = self._slippage(o.asset_class)

        if o.order_type == "limit":
            if o.limit_price is None or not (bar.low <= o.limit_price <= bar.high):
                return None
            return o.limit_price                          # no price improvement

        if o.order_type == "market":
            return bar.open * (1 + slip) if o.side == "buy" else bar.open * (1 - slip)

        if o.order_type == "stop":
            if o.side == "buy" and bar.high >= o.stop_price:
                return max(o.stop_price, bar.open) * (1 + slip)
            if o.side == "sell" and bar.low <= o.stop_price:
                return min(o.stop_price, bar.open) * (1 - slip)
            return None

        return None
