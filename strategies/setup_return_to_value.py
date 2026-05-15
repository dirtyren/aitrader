from __future__ import annotations
from typing import Optional

from core.acceptance import accepted_above, accepted_below
from core.bar import Bar
from core.session import SessionContext
from strategies.base_setup import BaseSetup, SetupSignal


class ReturnToValueSetup(BaseSetup):
    name = "return_to_value"

    def __init__(self, symbol: str, atr_mult_stop: float = 1.0,
                 arm_window_bars: int = 6, accept_n: int = 2,
                 accept_distance_atr: float = 0.25,
                 retrace_proximity_atr: float = 0.1):
        super().__init__(symbol)
        self.atr_mult_stop = atr_mult_stop
        self.arm_window_bars = arm_window_bars
        self.accept_n = accept_n
        self.accept_distance_atr = accept_distance_atr
        self.retrace_proximity_atr = retrace_proximity_atr
        self._side: Optional[str] = None     # direction of the trade we'd take (opposite of prior discovery)
        self._level: Optional[float] = None  # band that the price re-entered through

    def reset(self) -> None:
        super().reset()
        self._side = None
        self._level = None

    def check(self, ctx: SessionContext) -> Optional[SetupSignal]:
        if ctx.bar_count < 2:
            return None
        bar = ctx.bars[-1]
        prev = ctx.bars[-2]
        atr = ctx.atr() or 0.0
        if atr <= 0:
            return None

        upper, lower = ctx.upper_band, ctx.lower_band
        prev_was_outside_up = prev.close > upper
        prev_was_outside_dn = prev.close < lower
        now_in = lower <= bar.close <= upper

        # IDLE → REJECTION (re-entered value area)
        if self.state == "IDLE":
            if prev_was_outside_up and now_in:
                self.state, self._side, self._level = "REJECTION", "short", upper
                self.bars_in_state = 1
            elif prev_was_outside_dn and now_in:
                self.state, self._side, self._level = "REJECTION", "long", lower
                self.bars_in_state = 1
            return None

        # REJECTION → REENTRY_ACCEPTED (n closes inside value with distance)
        if self.state == "REJECTION":
            self.bars_in_state += 1
            inside = self._side == "short" and accepted_below(ctx.bars, self._level,
                                                              self.accept_n, self.accept_distance_atr, atr)
            inside = inside or (self._side == "long" and accepted_above(ctx.bars, self._level,
                                                                        self.accept_n, self.accept_distance_atr, atr))
            if inside:
                self.state = "REENTRY_ACCEPTED"
            return None

        # REENTRY_ACCEPTED → ARMED (retest band from inside)
        if self.state == "REENTRY_ACCEPTED":
            self.bars_in_state += 1
            close_to = abs(bar.close - self._level) <= self.retrace_proximity_atr * atr
            wick_to = (self._side == "short" and bar.high >= self._level - self.retrace_proximity_atr * atr) \
                   or (self._side == "long" and bar.low <= self._level + self.retrace_proximity_atr * atr)
            if close_to or wick_to:
                self.state = "ARMED"
                self.armed_level = self._level
                self.bars_in_state = 0
            return None

        # ARMED → FILLED | EXPIRED
        if self.state == "ARMED":
            self.bars_in_state += 1
            if self._side == "short":
                if bar.high >= self._level and bar.close <= self._level:
                    sig = self._build_signal(bar, atr, ctx.vwap)
                    self.reset()
                    return sig
                if bar.close > ctx.upper_band:        # broke out again — abort
                    self.reset()
            else:
                if bar.low <= self._level and bar.close >= self._level:
                    sig = self._build_signal(bar, atr, ctx.vwap)
                    self.reset()
                    return sig
                if bar.close < ctx.lower_band:
                    self.reset()
            if self.bars_in_state >= self.arm_window_bars:
                self.reset()
            return None
        return None

    def _build_signal(self, bar: Bar, atr: float, target_vwap: float) -> SetupSignal:
        if self._side == "short":
            entry = self._level
            stop = self._level + 0.5 * atr
        else:
            entry = self._level
            stop = self._level - 0.5 * atr
        return SetupSignal(
            setup=self.name, symbol=self.symbol, side=self._side,
            entry=entry, stop=stop, target=target_vwap,
            atr=atr, level=self._level, ts=bar.ts, notes={"target": "vwap"},
        )
