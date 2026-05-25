from __future__ import annotations
from typing import Optional

from core.bar import Bar
from core.session import SessionContext
from strategies.base_setup import BaseSetup, SetupSignal


class InitialBalanceSetup(BaseSetup):
    name = "initial_balance"

    def __init__(self, symbol: str, ib_bars: int = 6,
                 atr_mult_stop: float = 1.0, target_R: float = 2.0):
        super().__init__(symbol)
        self.ib_bars = ib_bars
        self.atr_mult_stop = atr_mult_stop
        self.target_R = target_R
        self._ib_high: Optional[float] = None
        self._ib_low: Optional[float] = None

    def reset(self) -> None:
        super().reset()
        self._ib_high = None
        self._ib_low = None

    def check(self, ctx: SessionContext) -> Optional[SetupSignal]:
        if ctx.bar_count < self.ib_bars + 1:
            return None

        if self._ib_high is None or self._ib_low is None:
            self._ib_high = max(b.high for b in ctx.bars[:self.ib_bars])
            self._ib_low = min(b.low for b in ctx.bars[:self.ib_bars])

        bar = ctx.bars[-1]
        prev_bar = ctx.bars[-2]
        atr = ctx.atr() or (bar.close * 0.01)

        if self.state == "IDLE":
            if ctx.regime == "Trend":
                if prev_bar.close <= self._ib_high < bar.close:
                    entry = bar.close
                    stop = entry - self.atr_mult_stop * atr
                    target = entry + self.target_R * (entry - stop)
                    return SetupSignal(
                        setup=self.name, symbol=self.symbol, side="long",
                        entry=entry, stop=stop, target=target,
                        atr=atr, level=self._ib_high, ts=bar.ts,
                        notes={"style": "trend_breakout", "ib_high": self._ib_high, "ib_low": self._ib_low}
                    )
                elif prev_bar.close >= self._ib_low > bar.close:
                    entry = bar.close
                    stop = entry + self.atr_mult_stop * atr
                    target = entry - self.target_R * (stop - entry)
                    return SetupSignal(
                        setup=self.name, symbol=self.symbol, side="short",
                        entry=entry, stop=stop, target=target,
                        atr=atr, level=self._ib_low, ts=bar.ts,
                        notes={"style": "trend_breakout", "ib_high": self._ib_high, "ib_low": self._ib_low}
                    )
            elif ctx.regime in ("Range", "Balance", "Undefined", "Discovery"):
                if prev_bar.high > self._ib_high and bar.close < self._ib_high:
                    entry = bar.close
                    stop = max(b.high for b in ctx.bars[-3:]) + 0.1 * atr
                    target = ctx.vwap
                    return SetupSignal(
                        setup=self.name, symbol=self.symbol, side="short",
                        entry=entry, stop=stop, target=target,
                        atr=atr, level=self._ib_high, ts=bar.ts,
                        notes={"style": "mean_reversion_fade", "ib_high": self._ib_high, "ib_low": self._ib_low}
                    )
                elif prev_bar.low < self._ib_low and bar.close > self._ib_low:
                    entry = bar.close
                    stop = min(b.low for b in ctx.bars[-3:]) - 0.1 * atr
                    target = ctx.vwap
                    return SetupSignal(
                        setup=self.name, symbol=self.symbol, side="long",
                        entry=entry, stop=stop, target=target,
                        atr=atr, level=self._ib_low, ts=bar.ts,
                        notes={"style": "mean_reversion_fade", "ib_high": self._ib_high, "ib_low": self._ib_low}
                    )
        return None
