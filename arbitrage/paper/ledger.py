from __future__ import annotations

from arbitrage.paper.order_sink import Fill


class Ledger:
    def __init__(self) -> None:
        self._fills: list[Fill] = []

    def record(self, fill: Fill) -> None:
        self._fills.append(fill)

    def realized_pnl_usd(self) -> float:
        return sum(f.pnl_usd for f in self._fills)

    def round_trips(self) -> int:
        return len(self._fills)
