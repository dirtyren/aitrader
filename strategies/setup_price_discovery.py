from __future__ import annotations
from typing import Optional

from core.acceptance import accepted_above, accepted_below
from core.bar import Bar
from core.session import SessionContext
from strategies.base_setup import BaseSetup, SetupSignal


class PriceDiscoverySetup(BaseSetup):
    name = "price_discovery"

    def __init__(self, symbol: str, atr_mult_stop: float = 1.0,
                 target_R: float = 1.5, arm_window_bars: int = 6,
                 accept_n: int = 2, accept_distance_atr: float = 0.25,
                 retrace_proximity_atr: float = 0.1):
        super().__init__(symbol)
        self.atr_mult_stop = atr_mult_stop
        self.target_R = target_R
        self.arm_window_bars = arm_window_bars
        self.accept_n = accept_n
        self.accept_distance_atr = accept_distance_atr
        self.retrace_proximity_atr = retrace_proximity_atr
        self._side: Optional[str] = None
        self._level: Optional[float] = None    # band breached at acceptance time

    def reset(self) -> None:
        super().reset()
        self._side = None
        self._level = None

    def check(self, ctx: SessionContext) -> Optional[SetupSignal]:
        if ctx.bar_count == 0:
            return None
        bar = ctx.bars[-1]
        atr = ctx.atr() or 0.0
        if atr <= 0:
            return None

        upper, lower = ctx.upper_band, ctx.lower_band
        in_value = lower <= bar.close <= upper

        # IDLE → BREAKOUT_PENDING
        if self.state == "IDLE":
            if bar.close > upper:
                self.state, self._side, self._level = "BREAKOUT_PENDING", "long", upper
                self.bars_in_state = 1
            elif bar.close < lower:
                self.state, self._side, self._level = "BREAKOUT_PENDING", "short", lower
                self.bars_in_state = 1
            return None

        # BREAKOUT_PENDING → ACCEPTED | IDLE
        if self.state == "BREAKOUT_PENDING":
            self.bars_in_state += 1
            if self._side == "long":
                if accepted_above(ctx.bars, self._level, self.accept_n,
                                  self.accept_distance_atr, atr):
                    self.state = "ACCEPTED"
                elif in_value:
                    self.reset()
            else:
                if accepted_below(ctx.bars, self._level, self.accept_n,
                                  self.accept_distance_atr, atr):
                    self.state = "ACCEPTED"
                elif in_value:
                    self.reset()
            return None

        # ACCEPTED → ARMED (price retraces toward the breached level)
        if self.state == "ACCEPTED":
            self.bars_in_state += 1
            close_to_level = abs(bar.close - self._level) <= self.retrace_proximity_atr * atr
            if close_to_level or (self._side == "long" and bar.low <= self._level + self.retrace_proximity_atr * atr) \
                              or (self._side == "short" and bar.high >= self._level - self.retrace_proximity_atr * atr):
                self.state = "ARMED"
                self.armed_level = self._level
                self.bars_in_state = 0
            return None

        # ARMED → FILLED | EXPIRED
        if self.state == "ARMED":
            self.bars_in_state += 1
            # Fire on a candle that wicks into the band and closes back in trend direction.
            if self._side == "long":
                if bar.low <= self._level and bar.close >= self._level and bar.is_bullish:
                    sig = self._build_signal(bar, atr)
                    self.reset()
                    return sig
                if bar.close <= ctx.lower_band:
                    self.reset()
            else:
                if bar.high >= self._level and bar.close <= self._level and not bar.is_bullish:
                    sig = self._build_signal(bar, atr)
                    self.reset()
                    return sig
                if bar.close >= ctx.upper_band:
                    self.reset()
            if self.bars_in_state >= self.arm_window_bars:
                self.reset()
            return None
        return None

    def _build_signal(self, bar: Bar, atr: float) -> SetupSignal:
        if self._side == "long":
            entry = self._level
            stop = bar.low - 0.1 * atr   # beyond the testing candle
            risk = entry - stop
            target = entry + self.target_R * risk
        else:
            entry = self._level
            stop = bar.high + 0.1 * atr
            risk = stop - entry
            target = entry - self.target_R * risk
        return SetupSignal(
            setup=self.name, symbol=self.symbol, side=self._side,
            entry=entry, stop=stop, target=target,
            atr=atr, level=self._level, ts=bar.ts, notes={"phase": "backtest"},
        )
