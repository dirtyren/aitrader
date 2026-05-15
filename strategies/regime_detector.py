from __future__ import annotations
from dataclasses import dataclass

from core.session import SessionContext


@dataclass(frozen=True)
class RegimeConfig:
    trend_day_range_mult: float = 1.5
    trend_day_in_value_max: float = 0.30
    balance_day_in_value_min: float = 0.60
    min_bars: int = 6


class RegimeDetector:
    def __init__(self, cfg: RegimeConfig):
        self.cfg = cfg

    def classify(self, ctx: SessionContext) -> str:
        if ctx.bar_count < self.cfg.min_bars:
            return "Undefined"
        in_value = ctx.in_value_area_fraction()
        day_range = ctx.day_high - ctx.day_low
        avg = ctx.avg_range_20d if ctx.avg_range_20d > 0 else day_range
        range_mult = day_range / avg if avg > 0 else 1.0

        if range_mult >= self.cfg.trend_day_range_mult and in_value <= self.cfg.trend_day_in_value_max:
            return "Trend"
        if in_value >= self.cfg.balance_day_in_value_min:
            return "Range"
        if in_value < self.cfg.balance_day_in_value_min and range_mult < self.cfg.trend_day_range_mult:
            return "Discovery"
        return "Undefined"
