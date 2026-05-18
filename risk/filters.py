from __future__ import annotations
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterable, Optional

from state.daily_ledger import DailyLedger
from state.position_book import PositionBook
from strategies.base_setup import SetupSignal


@dataclass(frozen=True)
class FilterResult:
    passed: bool
    reason: str = ""

    @classmethod
    def ok(cls) -> "FilterResult":
        return cls(passed=True, reason="")

    @classmethod
    def reject(cls, reason: str) -> "FilterResult":
        return cls(passed=False, reason=reason)


@dataclass(frozen=True)
class NewsBlackout:
    start: datetime
    duration_min: int
    label: str

    @property
    def end(self) -> datetime:
        return self.start + timedelta(minutes=self.duration_min)


class EntryFilter(ABC):
    name: str = "filter"

    @abstractmethod
    def check(self, signal: SetupSignal, ctx, ledger, book) -> FilterResult:
        raise NotImplementedError


class SystemHaltedFilter(EntryFilter):
    name = "system_halted"

    def __init__(self, circuit_breaker, lock_file_path: str):
        self.cb = circuit_breaker
        self.lock_file_path = lock_file_path

    def check(self, signal, ctx, ledger, book) -> FilterResult:
        if os.path.exists(self.lock_file_path):
            return FilterResult.reject("lock file present")
        if getattr(self.cb, "level", 0) >= 2:
            return FilterResult.reject(f"circuit breaker L{self.cb.level}")
        return FilterResult.ok()


class SessionWindowFilter(EntryFilter):
    name = "session_window"

    def __init__(self, opening_blackout_min: int = 15,
                 now_fn: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        self.opening_blackout_min = opening_blackout_min
        self.now_fn = now_fn

    def check(self, signal, ctx, ledger, book) -> FilterResult:
        if ctx is None or ctx.session_start_ts is None:
            return FilterResult.ok()
        elapsed = (self.now_fn() - ctx.session_start_ts).total_seconds() / 60.0
        if elapsed < self.opening_blackout_min:
            return FilterResult.reject(f"opening blackout: {elapsed:.1f} < {self.opening_blackout_min} min")
        return FilterResult.ok()


class NewsBlackoutFilter(EntryFilter):
    name = "news_blackout"

    def __init__(self, windows: Iterable[NewsBlackout], pad_min: int = 5,
                 now_fn: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        self.windows = list(windows)
        self.pad = timedelta(minutes=pad_min)
        self.now_fn = now_fn

    def check(self, signal, ctx, ledger, book) -> FilterResult:
        now = self.now_fn()
        for w in self.windows:
            if (w.start - self.pad) <= now <= (w.end + self.pad):
                return FilterResult.reject(f"news blackout: {w.label}")
        return FilterResult.ok()


class VolumeDeficitFilter(EntryFilter):
    name = "volume_deficit"

    def __init__(self, deficit_pct: float = 0.30, lookback_bars: int = 6):
        self.deficit_pct = deficit_pct
        self.lookback_bars = lookback_bars

    def check(self, signal, ctx, ledger, book) -> FilterResult:
        if ctx is None or ctx.bar_count < self.lookback_bars:
            return FilterResult.ok()
        recent = ctx.bars[-self.lookback_bars:]
        recent_vol = sum(b.volume for b in recent) / len(recent)
        baseline = getattr(ctx, "avg_volume_per_bar", 0.0)
        if baseline <= 0:
            return FilterResult.ok()        # cannot evaluate
        if recent_vol < (1 - self.deficit_pct) * baseline:
            return FilterResult.reject(f"volume {recent_vol:.0f} < {(1-self.deficit_pct)*baseline:.0f}")
        return FilterResult.ok()


class ConsecutiveLossFilter(EntryFilter):
    name = "consecutive_loss"

    def __init__(self, limit: int = 2, scope: str = "per_symbol"):
        self.limit = limit
        self.scope = scope

    def check(self, signal, ctx, ledger: DailyLedger | None, book) -> FilterResult:
        if ledger is None:
            return FilterResult.ok()
        if self.scope == "system_wide":
            if ledger.consec_losses_system >= self.limit:
                return FilterResult.reject(f"system consecutive losses {ledger.consec_losses_system}")
        else:
            count = ledger.consecutive_losses_for(signal.symbol)
            if count >= self.limit:
                return FilterResult.reject(f"consecutive losses on {signal.symbol}: {count}")
        return FilterResult.ok()


class ConcurrentPositionFilter(EntryFilter):
    name = "concurrent_position"

    def __init__(self, max_concurrent: int = 4):
        self.max_concurrent = max_concurrent

    def check(self, signal, ctx, ledger, book: PositionBook | None) -> FilterResult:
        if book is None:
            return FilterResult.ok()
        if book.get(signal.symbol) is not None:
            return FilterResult.reject("position already open on this symbol")
        if book.count() >= self.max_concurrent:
            return FilterResult.reject(f"max concurrent positions ({self.max_concurrent}) reached")
        return FilterResult.ok()


class SetupCooldownFilter(EntryFilter):
    name = "setup_cooldown"

    def __init__(self, cooldown_bars: int = 12):
        self.cooldown_bars = cooldown_bars
        self._last_fire: dict[tuple[str, str], datetime] = {}

    def check(self, signal, ctx, ledger, book) -> FilterResult:
        key = (signal.symbol, signal.setup)
        last = self._last_fire.get(key)
        if last is None or ctx is None:
            self._last_fire[key] = signal.ts
            return FilterResult.ok()
        elapsed_min = (signal.ts - last).total_seconds() / 60.0
        bar_min = 5
        if elapsed_min < self.cooldown_bars * bar_min:
            return FilterResult.reject(f"setup cooldown: {elapsed_min:.0f} < {self.cooldown_bars * bar_min} min")
        self._last_fire[key] = signal.ts
        return FilterResult.ok()


class RiskBudgetFilter(EntryFilter):
    name = "risk_budget"

    def __init__(self, daily_open_risk_cap_pct: float = 0.02):
        self.cap_pct = daily_open_risk_cap_pct

    def check(self, signal, ctx, ledger: DailyLedger | None, book: PositionBook | None) -> FilterResult:
        if ledger is None or book is None:
            return FilterResult.ok()
        proposed_risk = abs(signal.entry - signal.stop)
        cap = ledger.equity * self.cap_pct
        existing = book.aggregate_open_risk_usd()
        if existing + proposed_risk > cap:
            return FilterResult.reject(f"risk budget: existing {existing:.0f} + new {proposed_risk:.0f} > cap {cap:.0f}")
        return FilterResult.ok()


class FilterPipeline:
    def __init__(self, filters: list[EntryFilter]):
        self.filters = filters

    def check(self, signal, ctx, ledger, book) -> FilterResult:
        for f in self.filters:
            res = f.check(signal, ctx, ledger, book)
            if not res.passed:
                return FilterResult(passed=False, reason=f"{f.name}: {res.reason}")
        return FilterResult.ok()
