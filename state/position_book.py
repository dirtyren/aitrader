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
    stop_px: float | None
    target_px: float | None
    opened_at: datetime
    order_id: str
    breakeven_moved: bool = False
    bars_held: int = 0
    stop_order_id: str | None = None    # bracket stop-leg id (equity); None for crypto / virtual / adopted-no-bracket
    target_order_id: str | None = None  # limit order id for crypto TP
    initial_stop_px: float | None = None  # original stop at entry; survives breakeven moves for R calc
    adopted: bool = False               # True for positions reconciled from broker (monitor-only)

    @property
    def initial_risk_per_share(self) -> float:
        ref = self.initial_stop_px if self.initial_stop_px is not None else self.stop_px
        if ref is None:
            return 0.0
        return abs(self.entry_px - ref)

    @property
    def risk_per_share(self) -> float:
        if self.stop_px is None:
            return 0.0
        return abs(self.entry_px - self.stop_px)

    @property
    def open_risk_usd(self) -> float:
        return self.risk_per_share * self.qty


class PositionBook:
    def __init__(self) -> None:
        self._positions: dict[str, OpenPosition] = {}
        # Symbols whose position closed during the current cycle. The engine
        # clears this at tick start; the executor consults it to skip same-cycle
        # bracket re-entries (Alpaca rejects bracket entries while the prior
        # closing order is still settling, and re-entering on the bar that just
        # stopped us out is rarely the intended behavior).
        self._just_exited: set[str] = set()

    def add(self, p: OpenPosition) -> None:
        if p.symbol in self._positions:
            raise ValueError(f"Position already open on {p.symbol}")
        self._positions[p.symbol] = p

    def get(self, symbol: str) -> OpenPosition | None:
        return self._positions.get(symbol)

    def close(self, symbol: str) -> OpenPosition | None:
        pos = self._positions.pop(symbol, None)
        if pos is not None:
            self._just_exited.add(symbol)
        return pos

    def was_just_exited(self, symbol: str) -> bool:
        return symbol in self._just_exited

    def clear_just_exited(self) -> None:
        self._just_exited.clear()

    def symbols(self) -> list[str]:
        return list(self._positions.keys())

    def count(self) -> int:
        return len(self._positions)

    def all(self) -> list[OpenPosition]:
        return list(self._positions.values())

    def aggregate_open_risk_usd(self) -> float:
        return sum(p.open_risk_usd for p in self._positions.values())
