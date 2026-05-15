from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class Bar:
    symbol: str
    ts: datetime           # bar close timestamp, timezone-aware UTC
    open: float
    high: float
    low: float
    close: float
    volume: float          # float — crypto fractional volume

    def __post_init__(self) -> None:
        if self.high < max(self.open, self.close, self.low):
            raise ValueError(f"Invalid bar: high {self.high} below O/C/L for {self.symbol} @ {self.ts}")
        if self.low > min(self.open, self.close, self.high):
            raise ValueError(f"Invalid bar: low {self.low} above O/C/H for {self.symbol} @ {self.ts}")
        if self.ts.tzinfo is None:
            raise ValueError(f"Bar.ts must be timezone-aware: {self.ts}")

    @property
    def range(self) -> float:
        return self.high - self.low

    @property
    def is_bullish(self) -> bool:
        return self.close > self.open

    @property
    def typical_price(self) -> float:
        return (self.high + self.low + self.close) / 3.0
