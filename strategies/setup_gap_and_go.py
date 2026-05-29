"""Gap-and-Go pre-market breakout setup.

State machine (one instance per qualified symbol per day):

    IDLE  → FILLED  → MANAGED → CLOSED
        │
        └── EXPIRED  (entry deadline passed without trigger)

The setup observes 1-min bars between the scanner cut (08:30 ET) and the
entry deadline (default 09:30 ET). On each closed bar:

  1. If the bar's high prints above the running pre-market high, the running
     high is updated and the bar is *skipped* (never chase a same-bar new high).
  2. The 5-bar trailing average volume is computed.
  3. The trigger fires when bar.close > premarket_high AND
     bar.volume >= volume_confirm_mult * avg_recent_vol.
  4. Slippage guard: reject when (entry - premarket_high) / premarket_high
     exceeds max_entry_slippage_pct/100.
  5. stop = max(premarket_low, entry - atr_mult_stop_cap * atr_14d).
  6. target = entry + target_R * (entry - stop).

The emitted SetupSignal carries notes={"extended_hours": True, ...} so that
OrderExecutor routes the entry as a plain extended-hours limit. The regular
bracket flow is bypassed; OCO attach is performed at 09:30 by
broker.post_open_attach.

Once an entry has been submitted (state == "FILLED"), check() returns None.
After the entry deadline, the state is set to "EXPIRED" and check() likewise
returns None for the rest of the day.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from core.session import SessionContext
from strategies.base_setup import BaseSetup, SetupSignal


class GapAndGoSetup(BaseSetup):
    """Pre-market breakout entry. Long-only in v1."""

    name = "gap_and_go"

    def __init__(
        self,
        symbol: str,
        *,
        premarket_high: float,
        premarket_low: float,
        atr_14d: float,
        entry_deadline: datetime,
        atr_mult_stop_cap: float = 2.0,
        target_R: float = 2.0,
        volume_confirm_mult: float = 2.0,
        max_entry_slippage_pct: float = 0.5,
        avg_volume_window: int = 5,
    ) -> None:
        super().__init__(symbol)
        if atr_14d <= 0:
            raise ValueError(f"atr_14d must be positive, got {atr_14d!r}")
        if premarket_high <= 0:
            raise ValueError(f"premarket_high must be positive, got {premarket_high!r}")
        if premarket_low <= 0 or premarket_low >= premarket_high:
            raise ValueError(
                f"premarket_low must be positive and < premarket_high "
                f"(low={premarket_low}, high={premarket_high})"
            )
        if entry_deadline.tzinfo is None:
            raise ValueError("entry_deadline must be timezone-aware")

        self.premarket_high = premarket_high
        self.premarket_low = premarket_low
        self.atr_14d = atr_14d
        self.entry_deadline = entry_deadline
        self.atr_mult_stop_cap = atr_mult_stop_cap
        self.target_R = target_R
        self.volume_confirm_mult = volume_confirm_mult
        self.max_entry_slippage_pct = max_entry_slippage_pct
        self.avg_volume_window = avg_volume_window

    def reset(self) -> None:  # pragma: no cover - one-shot per day
        super().reset()

    def check(self, ctx: SessionContext) -> Optional[SetupSignal]:
        if self.state in ("FILLED", "MANAGED", "CLOSED", "EXPIRED"):
            return None
        if not ctx.bars:
            return None

        bar = ctx.bars[-1]

        # Past the deadline — give up for the day.
        if bar.ts >= self.entry_deadline:
            self.state = "EXPIRED"
            return None

        # Compare the entry trigger against the pre-market high established
        # BEFORE this bar — this is the level the trade thesis depends on.
        prior_pmh = self.premarket_high

        # 1) Bars whose HIGH prints above the prior PMH but whose CLOSE stays
        #    at-or-below it are pure fakeouts: extend PMH for future bars and
        #    skip. (A bar that closes above prior_pmh — the genuine breakout —
        #    is allowed to proceed; rejecting it on the same-bar HOD would
        #    make any trigger impossible because close > PMH requires
        #    high > PMH.)
        if bar.high > prior_pmh and bar.close <= prior_pmh:
            self.premarket_high = bar.high
            return None

        # 2) Need at least one prior bar to compute a trailing volume average.
        if len(ctx.bars) < 2:
            return None

        # Use up to avg_volume_window priors immediately before this bar.
        prior = ctx.bars[-(self.avg_volume_window + 1):-1]
        if not prior:
            return None
        avg_recent_vol = sum(b.volume for b in prior) / len(prior)
        if avg_recent_vol <= 0:
            return None

        # 3) Trigger: close above prior PMH on confirming volume.
        if bar.close <= prior_pmh:
            return None
        if bar.volume < self.volume_confirm_mult * avg_recent_vol:
            # Close broke PMH but volume failed — treat as failed breakout and
            # extend PMH so the next bar's check uses the new level.
            self.premarket_high = max(prior_pmh, bar.high)
            return None

        # 4) Slippage guard against the prior PMH (the level the trade is
        #    sized off of).
        slippage_pct = (bar.close - prior_pmh) / prior_pmh * 100.0
        if slippage_pct > self.max_entry_slippage_pct:
            self.premarket_high = max(prior_pmh, bar.high)
            return None

        # 5) Stop = max(PML, entry - atr_cap * ATR). 6) Target = entry + R*(entry-stop).
        entry = bar.close
        stop_atr_floor = entry - self.atr_mult_stop_cap * self.atr_14d
        stop = max(self.premarket_low, stop_atr_floor)
        if stop >= entry:
            # Defensive: can happen on a wide-bar overshoot once PML is at/above
            # entry. Reject rather than emit a non-positive-risk signal.
            return None
        target = entry + self.target_R * (entry - stop)

        self.state = "FILLED"
        return SetupSignal(
            setup=self.name,
            symbol=self.symbol,
            side="long",
            entry=entry,
            stop=stop,
            target=target,
            atr=self.atr_14d,
            level=prior_pmh,
            ts=bar.ts,
            notes={
                "style": "gap_continuation",
                "premarket_high": prior_pmh,
                "premarket_low": self.premarket_low,
                "extended_hours": True,
            },
        )
