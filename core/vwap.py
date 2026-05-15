from __future__ import annotations
import math
from dataclasses import dataclass, field
from core.bar import Bar


@dataclass
class VWAPBands:
    sigma: float = 1.0
    _sum_pv: float = 0.0       # Σ typical_price × volume
    _sum_v: float = 0.0        # Σ volume
    _sum_p2v: float = 0.0      # Σ typical_price² × volume
    _bar_count: int = 0

    def reset(self) -> None:
        self._sum_pv = 0.0
        self._sum_v = 0.0
        self._sum_p2v = 0.0
        self._bar_count = 0

    def add(self, bar: Bar) -> None:
        tp = bar.typical_price
        v = bar.volume
        self._sum_pv += tp * v
        self._sum_v += v
        self._sum_p2v += tp * tp * v
        self._bar_count += 1

    @property
    def bar_count(self) -> int:
        return self._bar_count

    @property
    def vwap(self) -> float:
        if self._sum_v <= 0:
            return float("nan")
        return self._sum_pv / self._sum_v

    @property
    def variance(self) -> float:
        if self._sum_v <= 0:
            return 0.0
        mean = self.vwap
        # E[X²] − (E[X])²
        return max(0.0, self._sum_p2v / self._sum_v - mean * mean)

    @property
    def stdev(self) -> float:
        return math.sqrt(self.variance)

    @property
    def upper(self) -> float:
        return self.vwap + self.sigma * self.stdev

    @property
    def lower(self) -> float:
        return self.vwap - self.sigma * self.stdev
