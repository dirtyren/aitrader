"""
circuit_breakers.py — Tiered circuit breaker logic for the regime_trader system.

This is part of the FAIL-SAFE layer. It has ABSOLUTE VETO POWER and is hardcoded,
independent of the HMM model's opinion.
"""

import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Optional


class CircuitBreaker:
    """
    Tiered circuit breaker that monitors daily P&L and peak-to-valley drawdown.

    Level 0: All clear — full position sizing (multiplier=1.0)
    Level 1: Daily loss >= 2% — reduce position sizing 50% (multiplier=0.5)
    Level 2: Daily loss >= 3% — halt trading for 24 hours (multiplier=0.0)
    Level 3: Peak-to-valley drawdown >= 10% — emergency shutdown (sys.exit)
    """

    def __init__(
        self,
        peak_equity: float,
        daily_loss_limit_1: float = 0.02,
        daily_loss_limit_2: float = 0.03,
        drawdown_limit: float = 0.10,
    ):
        self.peak_equity = peak_equity
        self.daily_loss_limit_1 = daily_loss_limit_1
        self.daily_loss_limit_2 = daily_loss_limit_2
        self.drawdown_limit = drawdown_limit
        self._suspension = TradingSuspension()
        self.level: int = 0    # last computed by check(); read by RiskManager + dashboard

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def check(self, current_equity: float, daily_pnl_pct: float) -> dict:
        """
        Check all circuit breaker conditions and return a status dict.

        Parameters
        ----------
        current_equity : float
            Current portfolio equity value.
        daily_pnl_pct : float
            Today's P&L as a fraction of starting equity (negative = loss).
            e.g. -0.025 means a 2.5% daily loss.

        Returns
        -------
        dict with keys:
            level            : int   — 0=clear, 1=reduce, 2=halt, 3=emergency_shutdown
            action           : str   — human-readable description
            trading_suspended: bool
            multiplier       : float — position size multiplier
        """
        # Level 3 — peak-to-valley drawdown (checked first; most severe)
        drawdown = self.peak_to_valley_drawdown(current_equity)
        if drawdown >= self.drawdown_limit:
            self.level = 3
            self._emergency_shutdown()  # calls sys.exit(1) — code below unreachable
            # Defensive return for test environments that mock sys.exit
            return {
                "level": 3,
                "action": "EMERGENCY_SHUTDOWN",
                "trading_suspended": True,
                "multiplier": 0.0,
            }

        # Level 2 — daily loss >= 3%
        if daily_pnl_pct <= -self.daily_loss_limit_2:
            self._suspension = TradingSuspension(
                suspended=True,
                resume_time=datetime.now(timezone.utc) + timedelta(hours=24),
                reason="3% daily loss threshold",
            )
            self.level = 2
            return {
                "level": 2,
                "action": "HALT_24H",
                "trading_suspended": True,
                "multiplier": 0.0,
            }

        # Level 1 — daily loss >= 2%
        if daily_pnl_pct <= -self.daily_loss_limit_1:
            self.level = 1
            return {
                "level": 1,
                "action": "REDUCE_50",
                "trading_suspended": False,
                "multiplier": 0.5,
            }

        # Level 0 — all clear
        self.level = 0
        return {
            "level": 0,
            "action": "CLEAR",
            "trading_suspended": False,
            "multiplier": 1.0,
        }

    def _emergency_shutdown(self):
        """
        Atomically write a lock file to disk and terminate the process immediately.

        The lock file must be manually deleted after an incident review
        before trading can resume.
        """
        from datetime import datetime, timezone
        timestamp = datetime.now(timezone.utc).isoformat()
        reason = "10% peak-to-valley drawdown threshold breached"
        lock_path = os.environ.get("LOCK_FILE_PATH", "lock.file")
        try:
            tmp_path = lock_path + ".tmp"
            with open(tmp_path, "w") as fh:
                fh.write(f"LOCKED_AT={timestamp}\nREASON={reason}\n")
            os.replace(tmp_path, lock_path)  # atomic rename on POSIX
        except OSError as exc:
            print(f"CRITICAL: failed to write lock file: {exc}", file=sys.stderr)
        finally:
            sys.exit(1)

    def is_suspended(self) -> bool:
        """Return True if trading is currently suspended via a timed halt."""
        return self._suspension.is_active()

    def update_peak(self, current_equity: float):
        """Update peak equity if current_equity is a new high-water mark."""
        if current_equity > self.peak_equity:
            self.peak_equity = current_equity

    def peak_to_valley_drawdown(self, current_equity: float) -> float:
        """
        Returns the fractional drawdown from the recorded peak.

        Returns
        -------
        float
            (peak - current) / peak.  Always >= 0.  Returns 0.0 if peak is zero.
        """
        if self.peak_equity <= 0:
            return 0.0
        return (self.peak_equity - current_equity) / self.peak_equity


# ---------------------------------------------------------------------------
# TradingSuspension — simple state object for 24-hour halts
# ---------------------------------------------------------------------------


@dataclass
class TradingSuspension:
    """
    Tracks whether trading is currently suspended and, for time-limited halts,
    when the suspension expires.
    """

    suspended: bool = False
    resume_time: Optional[datetime] = None  # timezone-aware UTC datetime for 24h halts
    reason: str = ""

    def is_active(self) -> bool:
        """
        Returns True if trading is currently suspended.

        For time-limited suspensions (resume_time is set), automatically clears
        the suspension once the resume time has passed.
        """
        if not self.suspended:
            return False
        if self.resume_time and datetime.now(timezone.utc) >= self.resume_time:
            self.suspended = False
            return False
        return True

    @classmethod
    def halt_24h(cls, reason: str = "Daily loss limit breached") -> "TradingSuspension":
        """Factory: create a 24-hour suspension starting from now."""
        return cls(
            suspended=True,
            resume_time=datetime.now(timezone.utc) + timedelta(hours=24),
            reason=reason,
        )
