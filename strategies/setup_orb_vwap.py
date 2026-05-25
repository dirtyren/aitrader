from __future__ import annotations
from typing import Optional

from core.bar import Bar
from core.session import SessionContext
from strategies.base_setup import BaseSetup, SetupSignal


class ORBVWAPSetup(BaseSetup):
    name = "orb_vwap"

    def __init__(self, symbol: str, orb_bars: int = 3,
                 atr_mult_stop: float = 1.0, target_R: float = 2.0):
        super().__init__(symbol)
        self.orb_bars = orb_bars
        self.atr_mult_stop = atr_mult_stop
        self.target_R = target_R
        self._orb_high: Optional[float] = None
        self._orb_low: Optional[float] = None

    def reset(self) -> None:
        super().reset()
        self._orb_high = None
        self._orb_low = None

    def check(self, ctx: SessionContext) -> Optional[SetupSignal]:
        if ctx.bar_count < self.orb_bars + 1:
            return None

        if self._orb_high is None or self._orb_low is None:
            self._orb_high = max(b.high for b in ctx.bars[:self.orb_bars])
            self._orb_low = min(b.low for b in ctx.bars[:self.orb_bars])

        bar = ctx.bars[-1]
        prev_bar = ctx.bars[-2]
        atr = ctx.atr() or (bar.close * 0.01)

        if len(ctx.bars) < 2:
            return None

        prev_v_sum = sum(b.volume for b in ctx.bars[:-1])
        if prev_v_sum > 0:
            prev_vwap = sum(b.typical_price * b.volume for b in ctx.bars[:-1]) / prev_v_sum
        else:
            prev_vwap = ctx.vwap

        vwap_slope_positive = ctx.vwap > prev_vwap
        vwap_slope_negative = ctx.vwap < prev_vwap

        if self.state == "IDLE":
            if prev_bar.close <= self._orb_high < bar.close:
                if bar.close > ctx.vwap and vwap_slope_positive:
                    entry = bar.close
                    stop = entry - self.atr_mult_stop * atr
                    target = entry + self.target_R * (entry - stop)
                    return SetupSignal(
                        setup=self.name, symbol=self.symbol, side="long",
                        entry=entry, stop=stop, target=target,
                        atr=atr, level=self._orb_high, ts=bar.ts,
                        notes={"style": "momentum_breakout", "orb_high": self._orb_high, "orb_low": self._orb_low}
                    )
            elif prev_bar.close >= self._orb_low > bar.close:
                if bar.close < ctx.vwap and vwap_slope_negative:
                    entry = bar.close
                    stop = entry + self.atr_mult_stop * atr
                    target = entry - self.target_R * (stop - entry)
                    return SetupSignal(
                        setup=self.name, symbol=self.symbol, side="short",
                        entry=entry, stop=stop, target=target,
                        atr=atr, level=self._orb_low, ts=bar.ts,
                        notes={"style": "momentum_breakout", "orb_high": self._orb_high, "orb_low": self._orb_low}
                    )
        return None
