"""
Chaikin Money Flow (CMF) setup — accumulation/distribution strategy.

CMF measures buying/selling pressure over a configurable lookback window.
For precious metals (gold/silver), mean-reversion characteristics favor
entering at CMF extremes and exiting on reversion, with tight stops.

CMF = Sum(MF * Volume) / Sum(Volume)
 where MF = ((Close - Low) - (High - Close)) / (High - Low)
"""
from __future__ import annotations
from typing import Optional

from core.session import SessionContext
from strategies.base_setup import BaseSetup, SetupSignal


class CMFSetup(BaseSetup):
    """Chaikin Money Flow with threshold-based crossover signals.

    Long  — CMF crosses above a negative threshold (oversold accumulation starting).
    Short — CMF crosses below a positive threshold (overbought distribution starting).

    Config parameters (from YAML setups.cmf):
        period: int     — CMF lookback window (default 20)
        threshold: float — absolute CMF threshold for signal (default 0.10)
        atr_mult_stop: float — stop distance in ATR multiples
        target_R: float — target in R-multiples
    """

    name = "cmf"

    def __init__(
        self,
        symbol: str,
        period: int = 20,
        threshold: float = 0.10,
        atr_mult_stop: float = 0.5,
        target_R: float = 1.5,
    ):
        super().__init__(symbol)
        self.period = period
        self.threshold = threshold
        self.atr_mult_stop = atr_mult_stop
        self.target_R = target_R
        self._prev_cmf: Optional[float] = None

    def _calc_cmf(self, ctx: SessionContext) -> Optional[float]:
        """Compute CMF over the lookback period."""
        if ctx.bar_count < self.period + 1:
            return None

        bars = ctx.bars[-(self.period + 1):]
        mf_vol_sum = 0.0
        vol_sum = 0.0

        for bar in bars[-self.period:]:
            high = bar.high
            low = bar.low
            close = bar.close
            volume = bar.volume

            h_l = high - low
            if h_l == 0 or volume == 0:
                continue

            mf = ((close - low) - (high - close)) / h_l
            mf_vol_sum += mf * volume
            vol_sum += volume

        if vol_sum == 0:
            return 0.0
        return mf_vol_sum / vol_sum

    def check(self, ctx: SessionContext) -> Optional[SetupSignal]:
        if ctx.bar_count == 0:
            return None

        cmf = self._calc_cmf(ctx)
        if cmf is None:
            return None

        bar = ctx.bars[-1]
        atr = ctx.atr() or (bar.close * 0.01)

        prev = self._prev_cmf
        self._prev_cmf = cmf

        # Need previous value for crossover detection
        if prev is None:
            return None

        if self.state == "IDLE":
            # Long: CMF crosses above -threshold (oversold → accumulation)
            if prev <= -self.threshold and cmf > -self.threshold:
                entry = bar.close
                stop_dist = self.atr_mult_stop * atr
                stop = entry - stop_dist
                target = entry + (stop_dist * self.target_R)

                self.reset()
                return SetupSignal(
                    setup=self.name,
                    symbol=self.symbol,
                    side="long",
                    entry=entry,
                    stop=stop,
                    target=target,
                    atr=atr,
                    level=entry,
                    ts=bar.ts,
                    notes={
                        "strategy": "cmf",
                        "cmf_val": cmf,
                        "prev_cmf": prev,
                        "period": self.period,
                        "threshold": self.threshold,
                    },
                )

            # Short: CMF crosses below +threshold (overbought → distribution)
            if prev >= self.threshold and cmf < self.threshold:
                entry = bar.close
                stop_dist = self.atr_mult_stop * atr
                stop = entry + stop_dist
                target = entry - (stop_dist * self.target_R)

                self.reset()
                return SetupSignal(
                    setup=self.name,
                    symbol=self.symbol,
                    side="short",
                    entry=entry,
                    stop=stop,
                    target=target,
                    atr=atr,
                    level=entry,
                    ts=bar.ts,
                    notes={
                        "strategy": "cmf",
                        "cmf_val": cmf,
                        "prev_cmf": prev,
                        "period": self.period,
                        "threshold": self.threshold,
                    },
                )

        return None

    def reset(self) -> None:
        super().reset()
        self._prev_cmf = None
