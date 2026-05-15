from __future__ import annotations
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from core.asset_class import AssetClassConfig, session_start_for
from core.atr import atr as compute_atr
from core.bar import Bar
from core.vwap import VWAPBands

logger = logging.getLogger(__name__)


@dataclass
class SessionContext:
    symbol: str
    asset_class: AssetClassConfig
    sigma: float = 1.0
    bars: list[Bar] = field(default_factory=list)
    vwap_bands: VWAPBands = field(init=False)
    session_start_ts: Optional[datetime] = None
    day_high: float = float("-inf")
    day_low: float = float("inf")
    avg_range_20d: float = 0.0          # populated externally; default 0 = unknown
    regime: str = "Undefined"
    touch_counts: dict[float, int] = field(default_factory=dict)

    def __post_init__(self):
        self.vwap_bands = VWAPBands(sigma=self.sigma)

    @property
    def bar_count(self) -> int:
        return len(self.bars)

    @property
    def vwap(self) -> float:
        return self.vwap_bands.vwap

    @property
    def upper_band(self) -> float:
        return self.vwap_bands.upper

    @property
    def lower_band(self) -> float:
        return self.vwap_bands.lower

    def reset(self, new_session_start: datetime) -> None:
        self.bars = []
        self.vwap_bands.reset()
        self.session_start_ts = new_session_start
        self.day_high = float("-inf")
        self.day_low = float("inf")
        self.regime = "Undefined"
        self.touch_counts = {}

    def ingest(self, bar: Bar) -> None:
        boundary = session_start_for(bar.ts, self.asset_class)
        if self.session_start_ts is None or boundary != self.session_start_ts:
            self.reset(boundary)

        self.bars.append(bar)
        self.vwap_bands.add(bar)
        self.day_high = max(self.day_high, bar.high)
        self.day_low = min(self.day_low, bar.low)

    def atr(self, window: int = 14) -> float:
        return compute_atr(self.bars, window)

    def in_value_area(self, price: float) -> bool:
        return self.lower_band <= price <= self.upper_band

    def in_value_area_fraction(self) -> float:
        """Fraction of bars whose CLOSE was inside the live value area at insertion time.

        Cheap approximation: uses current bands (not historical band evolution).
        Sufficient for regime classification.
        """
        if not self.bars:
            return 0.0
        inside = sum(1 for b in self.bars if self.lower_band <= b.close <= self.upper_band)
        return inside / len(self.bars)

    def fraction_above_vwap(self) -> float:
        if not self.bars:
            return 0.0
        above = sum(1 for b in self.bars if b.close > self.vwap)
        return above / len(self.bars)
