from __future__ import annotations
from typing import Optional

from core.bar import Bar
from core.session import SessionContext
from strategies.base_setup import BaseSetup, SetupSignal


class FadeExtremeSetup(BaseSetup):
    name = "fade_extreme"

    def __init__(self, symbol: str, atr_mult_stop: float = 0.75,
                 min_in_value_bars: int = 6,
                 scale_offsets_atr: list[float] | None = None,
                 scale_weights: list[float] | None = None,
                 balance_in_value_min: float = 0.60):
        super().__init__(symbol)
        self.atr_mult_stop = atr_mult_stop
        self.min_in_value_bars = min_in_value_bars
        self.scale_offsets_atr = scale_offsets_atr or [0.0, 0.25, 0.5]
        self.scale_weights = scale_weights or [0.4, 0.35, 0.25]
        self.balance_in_value_min = balance_in_value_min

    def _is_balance_day(self, ctx: SessionContext) -> bool:
        return ctx.in_value_area_fraction() >= self.balance_in_value_min

    def check(self, ctx: SessionContext) -> Optional[SetupSignal]:
        if ctx.bar_count < self.min_in_value_bars:
            return None
        if not self._is_balance_day(ctx):
            return None
        atr = ctx.atr() or 0.0
        if atr <= 0:
            return None

        bar = ctx.bars[-1]
        upper, lower, vwap = ctx.upper_band, ctx.lower_band, ctx.vwap

        # Rejection at upper band — short
        if bar.high > upper and bar.close < upper:
            entry = bar.close
            stop = upper + self.atr_mult_stop * atr
            return SetupSignal(
                setup=self.name, symbol=self.symbol, side="short",
                entry=entry, stop=stop, target=vwap,
                atr=atr, level=upper, ts=bar.ts,
                notes={"scale_offsets_atr": self.scale_offsets_atr,
                       "scale_weights": self.scale_weights, "scale_index": 0},
            )

        # Rejection at lower band — long
        if bar.low < lower and bar.close > lower:
            entry = bar.close
            stop = lower - self.atr_mult_stop * atr
            return SetupSignal(
                setup=self.name, symbol=self.symbol, side="long",
                entry=entry, stop=stop, target=vwap,
                atr=atr, level=lower, ts=bar.ts,
                notes={"scale_offsets_atr": self.scale_offsets_atr,
                       "scale_weights": self.scale_weights, "scale_index": 0},
            )
        return None
