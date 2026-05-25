from __future__ import annotations
from typing import Optional
import numpy as np

from core.session import SessionContext
from strategies.base_setup import BaseSetup, SetupSignal

class RSISetup(BaseSetup):
    name = "rsi_reversion"

    def __init__(self, symbol: str, threshold: float = 30.0,
                 direction: str = "long", stop_loss_pct: float = 2.0,
                 position_size_r: float = 0.5, period: int = 14):
        super().__init__(symbol)
        self.threshold = threshold
        self.direction = direction
        self.stop_loss_pct = stop_loss_pct / 100.0
        self.position_size_r = position_size_r
        self.period = period

    def _calculate_rsi(self, ctx: SessionContext) -> Optional[float]:
        if ctx.bar_count < self.period + 1:
            return None
            
        closes = [b.close for b in ctx.bars[-(self.period + 2):]]
        changes = np.diff(closes)
        
        gains = np.where(changes > 0, changes, 0)
        losses = np.where(changes < 0, -changes, 0)
        
        avg_gain = np.mean(gains[-self.period:])
        avg_loss = np.mean(losses[-self.period:])
        
        if avg_loss == 0:
            return 100.0
            
        rs = avg_gain / avg_loss
        return 100.0 - (100.0 / (1.0 + rs))

    def check(self, ctx: SessionContext) -> Optional[SetupSignal]:
        if ctx.bar_count == 0:
            return None
            
        rsi = self._calculate_rsi(ctx)
        if rsi is None:
            return None
            
        bar = ctx.bars[-1]
        atr = ctx.atr() or (bar.close * 0.01)

        # IDLE -> FIRE
        if self.state == "IDLE":
            if self.direction == "long" and rsi < self.threshold:
                entry = bar.close
                stop = entry * (1 - self.stop_loss_pct)
                target = entry + ((entry - stop) * 1.5) # Generic 1.5R target
                
                self.reset() # Reset state
                return SetupSignal(
                    setup=self.name, symbol=self.symbol, side="long",
                    entry=entry, stop=stop, target=target,
                    atr=atr, level=entry, ts=bar.ts, notes={"strategy": "rsi", "rsi_val": rsi, "position_size_r": self.position_size_r}
                )
                
            elif self.direction == "short" and rsi > (100 - self.threshold):
                entry = bar.close
                stop = entry * (1 + self.stop_loss_pct)
                target = entry - ((stop - entry) * 1.5)
                
                self.reset()
                return SetupSignal(
                    setup=self.name, symbol=self.symbol, side="short",
                    entry=entry, stop=stop, target=target,
                    atr=atr, level=entry, ts=bar.ts, notes={"strategy": "rsi", "rsi_val": rsi, "position_size_r": self.position_size_r}
                )
        return None
