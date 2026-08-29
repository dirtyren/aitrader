"""Opening Drive entry trigger: OR-high reclaim with volume confirmation.

Constructed only for symbols that passed the 10:00 scanner cut, so the setup
starts ARMED rather than IDLE.

No separate pullback state is required. or_high is the high of the 09:30-10:00
window, so or_close <= or_high holds by definition, and the min_clv gate
guarantees the close sits in the upper part of the range. Every candidate
therefore begins below its own trigger level; the machine only has to wait for
a post-cut bar to close above it.

States: ARMED -> FILLED (signal emitted) or ARMED -> EXPIRED (deadline passed).

SIDE. ``side`` selects what ACTION the trigger takes; it changes nothing about
what counts as a trigger. Detection (post-cut close above or_high, volume
confirmation, close above session VWAP), the structural pullback low, and the
risk floor / ceiling rules are identical on both sides — only the placement of
stop and target is mirrored around the entry. ``long`` is the production
default and the live trader never sets anything else; ``short`` exists so
scripts/backtest_opening_drive.py --side short can test the inverted
hypothesis (large-cap opening-range extensions mean-revert) against the exact
same screen and trigger, with directly comparable R.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from core.session import SessionContext
from strategies.base_setup import BaseSetup, SetupSignal

logger = logging.getLogger(__name__)


class OpeningDriveSetup(BaseSetup):
    name = "opening_drive"

    def __init__(
        self,
        symbol: str,
        or_high: float,
        or_low: float,
        atr_14d: float,
        avg_minute_volume: float,
        entry_deadline: datetime,
        volume_confirm_mult: float = 2.0,
        target_R: float = 2.0,
        min_stop_atr_frac: float = 0.15,
        atr_mult_stop_cap: float = 2.0,
        side: str = "long",
    ) -> None:
        if side not in ("long", "short"):
            raise ValueError(f"side must be 'long' or 'short', got {side!r}")
        super().__init__(symbol)
        self.or_high = or_high
        self.or_low = or_low
        self.atr_14d = atr_14d
        self.avg_minute_volume = avg_minute_volume
        self.entry_deadline = entry_deadline
        self.volume_confirm_mult = volume_confirm_mult
        self.target_R = target_R
        self.min_stop_atr_frac = min_stop_atr_frac
        self.atr_mult_stop_cap = atr_mult_stop_cap
        self.side = side
        self.state = "ARMED"
        self._run_low: float = float("inf")

    def reset(self) -> None:
        super().reset()
        self.state = "ARMED"
        self._run_low = float("inf")

    def check(self, ctx: SessionContext) -> Optional[SetupSignal]:
        if not ctx.bars:
            return None
        if self.state != "ARMED":
            return None

        bar = ctx.bars[-1]

        if bar.ts >= self.entry_deadline:
            self.state = "EXPIRED"
            logger.info("OD_SETUP_EXPIRED symbol=%s deadline=%s",
                        self.symbol, self.entry_deadline.isoformat())
            return None

        # Track the retracement low across the whole entry window, including
        # the trigger bar itself — this becomes the structural stop.
        self._run_low = min(self._run_low, bar.low)

        if bar.close <= self.or_high:
            return None
        if bar.volume < self.volume_confirm_mult * self.avg_minute_volume:
            return None
        if bar.close <= ctx.vwap:
            return None

        entry = bar.close
        structural_low = self._run_low
        risk = entry - structural_low

        max_risk = self.atr_mult_stop_cap * self.atr_14d
        if risk > max_risk:
            # Reject rather than clamp: clamping tighter than structure
            # defeats the point of a structural stop, and a retracement this
            # deep simply means the R:R is not there.
            logger.info(
                "OD_TRIGGER_REJECTED_WIDE_STOP symbol=%s risk=%.4f cap=%.4f",
                self.symbol, risk, max_risk,
            )
            return None

        min_risk = self.min_stop_atr_frac * self.atr_14d
        stop_floored = risk < min_risk
        if stop_floored:
            risk = min_risk

        # ``risk`` above is the SAME magnitude on both sides — the structural
        # pullback distance after the floor and the ceiling rule. Only the
        # direction it is applied in flips, so R is directly comparable.
        if self.side == "short":
            stop = entry + risk
            target = entry - self.target_R * risk
        else:
            stop = entry - risk
            target = entry + self.target_R * risk
        self.state = "FILLED"

        return SetupSignal(
            setup=self.name, symbol=self.symbol, side=self.side,
            entry=entry, stop=stop, target=target,
            atr=self.atr_14d, level=self.or_high, ts=bar.ts,
            notes={
                "style": "or_high_reclaim",
                "or_high": self.or_high,
                "or_low": self.or_low,
                "structural_low": structural_low,
                "stop_floored": stop_floored,
                "trigger_volume": bar.volume,
                "avg_minute_volume": self.avg_minute_volume,
            },
        )
