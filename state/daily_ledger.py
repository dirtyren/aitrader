from __future__ import annotations
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass(frozen=True)
class TradeRecord:
    symbol: str
    setup: str
    entry_ts: datetime
    exit_ts: datetime
    entry_px: float
    exit_px: float
    side: str
    qty: float
    R_realized: float
    pnl_usd: float


@dataclass
class DailyLedger:
    initial_equity: float
    equity: float = field(init=False)
    day_pnl: float = 0.0
    day_started_at: Optional[datetime] = None
    trades_today: list[TradeRecord] = field(default_factory=list)
    consec_losses_per_symbol: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    consec_losses_system: int = 0

    def __post_init__(self):
        self.equity = self.initial_equity

    def record(self, t: TradeRecord) -> None:
        self.trades_today.append(t)
        self.equity += t.pnl_usd
        self.day_pnl += t.pnl_usd
        if t.pnl_usd < 0:
            self.consec_losses_per_symbol[t.symbol] = self.consec_losses_per_symbol.get(t.symbol, 0) + 1
            self.consec_losses_system += 1
        else:
            self.consec_losses_per_symbol[t.symbol] = 0
            self.consec_losses_system = 0

    def consecutive_losses_for(self, symbol: str) -> int:
        return self.consec_losses_per_symbol.get(symbol, 0)

    def roll_day(self, new_day_start: datetime) -> None:
        self.day_started_at = new_day_start
        self.day_pnl = 0.0
        self.trades_today = []
        self.consec_losses_per_symbol = defaultdict(int)
        self.consec_losses_system = 0
