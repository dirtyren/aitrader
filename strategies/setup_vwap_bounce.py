from __future__ import annotations
from typing import Optional

from core.bar import Bar
from core.session import SessionContext
from strategies.base_setup import BaseSetup, SetupSignal


class VWAPBounceSetup(BaseSetup):
    name = "vwap_bounce"

    def __init__(self, symbol: str, atr_mult_stop: float = 1.25,
                 target_R: float = 2.5, arm_window_bars: int = 4,
                 trend_majority: float = 0.7, trend_range_mult: float = 1.5,
                 retrace_proximity_atr: float = 0.15):
        super().__init__(symbol)
        self.atr_mult_stop = atr_mult_stop
        self.target_R = target_R
        self.arm_window_bars = arm_window_bars
        self.trend_majority = trend_majority
        self.trend_range_mult = trend_range_mult
        self.retrace_proximity_atr = retrace_proximity_atr
        self._trap_bar: Optional[Bar] = None
        self._reclaim_bar: Optional[Bar] = None
        self._side: Optional[str] = None

    def reset(self) -> None:
        super().reset()
        self._trap_bar = None
        self._reclaim_bar = None
        self._side = None

    def _trend_side(self, ctx: SessionContext) -> Optional[str]:
        if ctx.bar_count < 6:
            return None
        avg = ctx.avg_range_20d if ctx.avg_range_20d > 0 else (ctx.day_high - ctx.day_low)
        if avg <= 0:
            return None
        if (ctx.day_high - ctx.day_low) < self.trend_range_mult * avg:
            return None
        frac_above = ctx.fraction_above_vwap()
        if frac_above >= self.trend_majority:
            return "long"
        if frac_above <= 1 - self.trend_majority:
            return "short"
        return None

    def check(self, ctx: SessionContext) -> Optional[SetupSignal]:
        if ctx.bar_count == 0:
            return None
        bar = ctx.bars[-1]
        atr = ctx.atr() or 0.0
        if atr <= 0:
            return None

        if self.state == "IDLE":
            side = self._trend_side(ctx)
            if side:
                self.state, self._side = "TREND_CONFIRMED", side
            return None

        if self.state == "TREND_CONFIRMED":
            if self._side == "long" and bar.low < ctx.vwap and bar.close < ctx.vwap:
                self._trap_bar = bar
                self.state = "SUB_VWAP_TRAP"
            elif self._side == "short" and bar.high > ctx.vwap and bar.close > ctx.vwap:
                self._trap_bar = bar
                self.state = "SUB_VWAP_TRAP"
            return None

        if self.state == "SUB_VWAP_TRAP":
            if self._side == "long" and bar.close > ctx.vwap:
                self._reclaim_bar = bar
                self.state = "ARMED"
                self.bars_in_state = 0
            elif self._side == "short" and bar.close < ctx.vwap:
                self._reclaim_bar = bar
                self.state = "ARMED"
                self.bars_in_state = 0
            return None

        if self.state == "ARMED":
            self.bars_in_state += 1
            close_to_vwap = abs(bar.close - ctx.vwap) <= self.retrace_proximity_atr * atr
            wick_to = (self._side == "long" and bar.low <= ctx.vwap + self.retrace_proximity_atr * atr) \
                   or (self._side == "short" and bar.high >= ctx.vwap - self.retrace_proximity_atr * atr)
            if close_to_vwap or wick_to:
                sig = self._build_signal(bar, atr, ctx.vwap)
                self.reset()
                return sig
            if self.bars_in_state >= self.arm_window_bars:
                self.reset()
            return None
        return None

    def _build_signal(self, bar: Bar, atr: float, vwap: float) -> SetupSignal:
        if self._side == "long":
            entry = vwap
            stop = (self._reclaim_bar.low if self._reclaim_bar else bar.low) - 0.1 * atr
            risk = entry - stop
            target = entry + self.target_R * risk
        else:
            entry = vwap
            stop = (self._reclaim_bar.high if self._reclaim_bar else bar.high) + 0.1 * atr
            risk = stop - entry
            target = entry - self.target_R * risk
        return SetupSignal(
            setup=self.name, symbol=self.symbol, side=self._side,
            entry=entry, stop=stop, target=target,
            atr=atr, level=vwap, ts=bar.ts, notes={"trend": True},
        )
