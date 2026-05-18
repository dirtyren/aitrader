from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime


@dataclass
class OpenPosition:
    symbol: str
    setup: str
    side: str               # "long" | "short"
    qty: float
    entry_px: float
    stop_px: float
    target_px: float
    opened_at: datetime
    order_id: str
    breakeven_moved: bool = False
    bars_held: int = 0
    stop_order_id: str | None = None    # bracket stop-leg id (equity); None for crypto / virtual
    initial_stop_px: float | None = None  # original stop at entry; survives breakeven moves for R calc

    @property
    def initial_risk_per_share(self) -> float:
        ref = self.initial_stop_px if self.initial_stop_px is not None else self.stop_px
        return abs(self.entry_px - ref)

    @property
    def risk_per_share(self) -> float:
        return abs(self.entry_px - self.stop_px)

    @property
    def open_risk_usd(self) -> float:
        return self.risk_per_share * self.qty


class PositionBook:
    def __init__(self) -> None:
        self._positions: dict[str, OpenPosition] = {}

    def add(self, p: OpenPosition) -> None:
        if p.symbol in self._positions:
            raise ValueError(f"Position already open on {p.symbol}")
        self._positions[p.symbol] = p

    def get(self, symbol: str) -> OpenPosition | None:
        return self._positions.get(symbol)

    def close(self, symbol: str) -> OpenPosition | None:
        return self._positions.pop(symbol, None)

    def symbols(self) -> list[str]:
        return list(self._positions.keys())

    def count(self) -> int:
        return len(self._positions)

    def all(self) -> list[OpenPosition]:
        return list(self._positions.values())

    def aggregate_open_risk_usd(self) -> float:
        return sum(p.open_risk_usd for p in self._positions.values())
