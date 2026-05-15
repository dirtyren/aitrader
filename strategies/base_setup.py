from __future__ import annotations
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from core.session import SessionContext


@dataclass(frozen=True)
class SetupSignal:
    setup: str                       # "price_discovery" | "fade_extreme" | ...
    symbol: str
    side: str                        # "long" | "short"
    entry: float                     # planned entry price (limit, market, or scale-in price)
    stop: float
    target: float
    atr: float
    level: float                     # the band/vwap level that triggered the setup
    ts: datetime
    notes: dict[str, object] = field(default_factory=dict)

    @property
    def risk_per_share(self) -> float:
        return abs(self.entry - self.stop)

    @property
    def reward_per_share(self) -> float:
        return abs(self.target - self.entry)

    @property
    def r_multiple_target(self) -> float:
        if self.risk_per_share == 0:
            return 0.0
        return self.reward_per_share / self.risk_per_share


class BaseSetup(ABC):
    """Abstract setup state machine.

    Subclasses keep their own per-symbol state across .check() calls.
    """

    name: str = ""

    def __init__(self, symbol: str) -> None:
        self.symbol = symbol
        self.state: str = "IDLE"
        self.armed_level: Optional[float] = None
        self.bars_in_state: int = 0

    @abstractmethod
    def check(self, ctx: SessionContext) -> Optional[SetupSignal]:
        """Run one bar-close evaluation. Return a signal when ARMED → FILLED transition occurs."""
        raise NotImplementedError

    def reset(self) -> None:
        self.state = "IDLE"
        self.armed_level = None
        self.bars_in_state = 0
