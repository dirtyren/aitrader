from __future__ import annotations
from typing import Optional

from core.bar import Bar
from core.session import SessionContext
from strategies.base_setup import BaseSetup, SetupSignal


class VWAPDevBandsSetup(BaseSetup):
    name = "vwap_dev_bands"

    def __init__(self, symbol: str, sigma: float = 2.5,
                 atr_mult_stop: float = 1.0, target_R: float = 2.0):
        super().__init__(symbol)
        self.sigma = sigma
        self.atr_mult_stop = atr_mult_stop
        self.target_R = target_R

    def check(self, ctx: SessionContext) -> Optional[SetupSignal]:
        if ctx.bar_count < 10:
            return None

        bar = ctx.bars[-1]
        prev_bar = ctx.bars[-2]
        stdev = ctx.vwap_bands.stdev
        vwap = ctx.vwap

        if stdev <= 0:
            return None

        upper_limit = vwap + self.sigma * stdev
        lower_limit = vwap - self.sigma * stdev
        atr = ctx.atr() or (bar.close * 0.01)

        if ctx.regime in ("Range", "Balance", "Undefined", "Discovery"):
            if self.state == "IDLE":
                if prev_bar.high > upper_limit and bar.close < upper_limit:
                    entry = bar.close
                    stop = entry + self.atr_mult_stop * atr
                    target = vwap
                    return SetupSignal(
                        setup=self.name, symbol=self.symbol, side="short",
                        entry=entry, stop=stop, target=target,
                        atr=atr, level=upper_limit, ts=bar.ts,
                        notes={"style": "vwap_band_reversion", "sigma": self.sigma, "vwap": vwap}
                    )
                elif prev_bar.low < lower_limit and bar.close > lower_limit:
                    entry = bar.close
                    stop = entry - self.atr_mult_stop * atr
                    target = vwap
                    return SetupSignal(
                        setup=self.name, symbol=self.symbol, side="long",
                        entry=entry, stop=stop, target=target,
                        atr=atr, level=lower_limit, ts=bar.ts,
                        notes={"style": "vwap_band_reversion", "sigma": self.sigma, "vwap": vwap}
                    )
        return None
