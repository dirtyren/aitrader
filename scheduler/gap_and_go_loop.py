"""Gap-and-Go daily lifecycle orchestrator.

Times below are America/New_York (the spec's reference timezone). Internal
timestamps are timezone-aware UTC throughout.

| Local time  | Action                                                |
|-------------|-------------------------------------------------------|
| 03:30       | Boot, validate Alpaca connection                      |
| 03:35       | Refresh baselines if stale (>baselines_max_age_days)  |
| 04:00       | Pre-market opens — snapshot poll loop starts          |
| 08:30       | Scanner cut, build per-symbol setups                  |
| 08:30→09:30 | 1-min bars on watchlist; setup watches for breakout   |
| 09:30       | Attach OCO to pre-market fills; switch to 5-min bars  |
| 09:30→15:55 | Managed phase via PositionManager                     |
| 15:55       | EOD flat — close any remaining Gap-and-Go positions   |
| 16:05       | Idle until next 03:30 boot                            |

This module exposes the orchestrator's pure phase-handler methods and a
run_day() driver. The intent is that each phase is independently testable
(no time.sleep or network in the phase methods themselves).
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone
from typing import Callable, Iterable

import pytz

from broker.post_open_attach import attach_brackets_for_premarket_fills
from core.asset_class import AssetClassConfig
from core.atr import atr as compute_atr
from core.bar import Bar
from core.session import SessionContext
from state.position_book import PositionBook
from strategies.gap_scanner import GapScanner, ScanResult
from strategies.setup_gap_and_go import GapAndGoSetup

logger = logging.getLogger(__name__)


_NY_TZ = pytz.timezone("America/New_York")


def _ny_dt(date: datetime, hh: int, mm: int) -> datetime:
    """Build a timezone-aware UTC datetime from an NY date + local HH:MM."""
    naive = datetime.combine(date.date(), time(hh, mm))
    return _NY_TZ.localize(naive).astimezone(timezone.utc)


@dataclass
class GapAndGoConfig:
    """Subset of the YAML settings the loop actually consumes."""
    universe_path: str
    baselines_path: str
    baselines_max_age_days: int = 7
    snapshot_poll_seconds: int = 300
    # Setup defaults
    atr_mult_stop_cap: float = 2.0
    target_R: float = 2.0
    volume_confirm_mult: float = 2.0
    max_entry_slippage_pct: float = 0.5
    entry_window_minutes: int = 60
    # Bar timeframe switch
    premarket_bar_timeframe: str = "1Min"
    regular_bar_timeframe: str = "5Min"
    # Risk
    max_concurrent_positions: int = 4


@dataclass
class _DayState:
    """Per-day mutable state; reset_for_new_day() at EOD."""
    cut_done: bool = False
    oco_attach_done: bool = False
    eod_close_done: bool = False
    watchlist: list[ScanResult] = field(default_factory=list)
    setups: dict[str, GapAndGoSetup] = field(default_factory=dict)
    contexts: dict[str, SessionContext] = field(default_factory=dict)


class GapAndGoLoop:
    """Coordinates one trading day for the Gap-and-Go strategy.

    Wired by main_gap_and_go.py. Tests drive the phase-handler methods directly
    without invoking run_day(); run_day() is the production driver.
    """

    def __init__(
        self,
        cfg: GapAndGoConfig,
        scanner: GapScanner,
        equity_asset_class: AssetClassConfig,
        alpaca_client,
        alpaca_data,
        risk_manager,
        executor,
        book: PositionBook,
        position_manager,
        ledger,
        strategy_name: str,
        snapshots_provider: Callable[[Iterable[str]], dict] | None = None,
    ) -> None:
        self.cfg = cfg
        self.scanner = scanner
        self.equity_asset_class = equity_asset_class
        self.alpaca = alpaca_client
        self.data = alpaca_data
        self.risk_manager = risk_manager
        self.executor = executor
        self.book = book
        self.position_manager = position_manager
        self.ledger = ledger
        self.strategy_name = strategy_name
        # Optional injection point for the bulk-snapshot caller; kept here so
        # tests can substitute a fixture without subclassing.
        self.snapshots_provider = snapshots_provider or self._default_snapshots
        self.day = _DayState()

    # ── Time helpers ────────────────────────────────────────────────────

    def cut_time(self, day: datetime) -> datetime:
        return _ny_dt(day, 8, 30)

    def deadline_time(self, day: datetime) -> datetime:
        # entry_window_minutes after the cut, capped at the regular open.
        cut = self.cut_time(day)
        return min(cut + timedelta(minutes=self.cfg.entry_window_minutes),
                   _ny_dt(day, 9, 30))

    def regular_open_time(self, day: datetime) -> datetime:
        return _ny_dt(day, 9, 30)

    def eod_close_time(self, day: datetime) -> datetime:
        return _ny_dt(day, 15, 55)

    # ── Phase 1: baseline refresh ───────────────────────────────────────

    def refresh_baselines_if_stale(self, now: datetime) -> bool:
        """Return True if baselines were refreshed this call.

        The actual baseline computation requires N daily bars + ~25 days of
        1-min pre-market bars per symbol via AlpacaData. To keep this
        method side-effect-light and testable, we delegate the per-symbol
        fetch to ``self._compute_baseline``, which subclasses or tests can
        substitute.
        """
        if not self.scanner.baselines_are_stale(now):
            return False
        logger.info("BASELINES_REFRESH_START symbols=%d age_days=%s",
                    len(self.scanner.universe),
                    self.scanner.baselines_age_days(now))
        refreshed: dict = {}
        for sym in self.scanner.universe:
            try:
                refreshed[sym] = self._compute_baseline(sym, now)
            except Exception as exc:
                logger.warning("BASELINE_REFRESH_FAILED symbol=%s error=%s",
                               sym, exc)
        if refreshed:
            self.scanner.baselines.update(refreshed)
            try:
                self.scanner.save_baselines(
                    self.scanner.baselines, self.cfg.baselines_path,
                )
            except OSError as exc:
                logger.error("BASELINES_SAVE_FAILED path=%s error=%s",
                             self.cfg.baselines_path, exc)
        logger.info("BASELINES_REFRESH_DONE refreshed=%d", len(refreshed))
        return True

    def _compute_baseline(self, symbol: str, now: datetime):
        """Default implementation — pulls 25 daily bars to derive ATR + ADV.

        Pre-market average volume is stubbed at zero unless the caller
        overrides; the production wiring in main_gap_and_go computes it from
        ~25 days of 1-min pre-market bars. Tests typically pre-populate the
        baselines dict and never call this method.
        """
        from strategies.gap_scanner import _Baseline

        end = now
        start = now - timedelta(days=40)
        bars = self.data.get_bars(symbol, "equity", "1Day",
                                  start=start, end=end, use_cache=False)
        if not bars:
            raise ValueError(f"no daily bars for {symbol}")
        atr_14 = compute_atr(bars[-15:], 14) if len(bars) >= 14 else compute_atr(bars, 14)
        if atr_14 <= 0:
            raise ValueError(f"non-positive ATR for {symbol}")
        recent = bars[-20:]
        adv = sum(b.volume for b in recent) / len(recent) if recent else 0.0
        return _Baseline(
            atr_14d=atr_14,
            avg_premarket_volume_20d=0.0,  # production override fills this
            avg_daily_volume_20d=adv,
            computed_at=now,
        )

    # ── Phase 2: pre-market snapshot polling ────────────────────────────

    def poll_snapshot(self, now: datetime) -> None:
        """Pull a single bulk snapshot and feed it into the scanner."""
        snapshots = self.snapshots_provider(self.scanner.universe)
        self.scanner.candidate_status(snapshots, now)

    def _default_snapshots(self, universe: Iterable[str]) -> dict:
        """Production hook — issue the bulk-snapshot HTTP call.

        Left as a thin pass-through so the AlpacaClient can grow a single
        snapshot endpoint method later without touching the loop. Right now
        we don't have a built-in helper; if the caller did not supply a
        ``snapshots_provider``, polling is a no-op (logged).
        """
        logger.warning("SNAPSHOT_PROVIDER_MISSING — pre-market polling disabled")
        return {}

    # ── Phase 3: scanner cut + setup build ──────────────────────────────

    def run_cut(self, now: datetime) -> list[ScanResult]:
        if self.day.cut_done:
            return self.day.watchlist
        watchlist = self.scanner.run_cut(now)
        self.day.watchlist = watchlist
        deadline = self.deadline_time(now)
        for r in watchlist:
            self.day.setups[r.symbol] = GapAndGoSetup(
                symbol=r.symbol,
                premarket_high=r.premarket_high,
                premarket_low=r.premarket_low,
                atr_14d=r.atr_14d,
                entry_deadline=deadline,
                atr_mult_stop_cap=self.cfg.atr_mult_stop_cap,
                target_R=self.cfg.target_R,
                volume_confirm_mult=self.cfg.volume_confirm_mult,
                max_entry_slippage_pct=self.cfg.max_entry_slippage_pct,
            )
            self.day.contexts[r.symbol] = SessionContext(
                symbol=r.symbol, asset_class=self.equity_asset_class,
            )
        self.day.cut_done = True
        logger.info("SCANNER_CUT_DONE n_candidates=%d deadline=%s",
                    len(watchlist), deadline.isoformat())
        return watchlist

    # ── Phase 4: per-bar entry detection ────────────────────────────────

    def on_bar(self, symbol: str, bar: Bar) -> None:
        """Push one freshly-closed bar through the symbol's setup + executor.

        Called per symbol on every 1-min boundary in 08:30->deadline. After
        09:30 the loop transitions to the managed phase (see manage_open).
        """
        ctx = self.day.contexts.get(symbol)
        setup = self.day.setups.get(symbol)
        if ctx is None or setup is None:
            return
        ctx.ingest(bar)
        signal = setup.check(ctx)
        if signal is None:
            return
        decision = self.risk_manager.evaluate(signal, ctx, "equity")
        if not decision.approved:
            logger.info("GAPGO_REJECTED symbol=%s reason=%s",
                        symbol, decision.reason)
            return
        logger.info("GAPGO_SIGNAL_FIRED symbol=%s entry=%.4f stop=%.4f target=%.4f",
                    symbol, signal.entry, signal.stop, signal.target)
        self.executor.submit(signal, decision, asset_class="equity")

    # ── Phase 5: 09:30 OCO attach + bar timeframe switch ────────────────

    def attach_premarket_brackets(self, now: datetime) -> dict:
        if self.day.oco_attach_done:
            return {"attached": 0, "failsafe_closed": 0, "skipped": 0}
        summary = attach_brackets_for_premarket_fills(
            self.book, self.alpaca,
            strategy_name=self.strategy_name, now=now,
        )
        self.day.oco_attach_done = True
        return summary

    def switch_to_regular_session_bars(self) -> None:
        """Reset each watchlist symbol's SessionContext.

        The pre-market context held 1-min bars; the regular session uses
        5-min bars. PositionManager.on_bar runs against this fresh context.
        """
        for sym in list(self.day.contexts.keys()):
            self.day.contexts[sym] = SessionContext(
                symbol=sym, asset_class=self.equity_asset_class,
            )

    # ── Phase 6: managed phase ──────────────────────────────────────────

    def manage_open(self, symbol: str, bar: Bar) -> None:
        """Ingest a regular-session bar and route any PositionManager actions."""
        ctx = self.day.contexts.get(symbol)
        if ctx is not None:
            ctx.ingest(bar)
        actions = self.position_manager.on_bar(symbol, bar)
        if actions:
            self.executor.handle_actions(actions, asset_class="equity")

    # ── Phase 7: EOD flat ───────────────────────────────────────────────

    def force_close_all(self, now: datetime) -> int:
        """Market-close every Gap-and-Go position. Returns count closed."""
        if self.day.eod_close_done:
            return 0
        closed = 0
        for pos in list(self.book.all()):
            if pos.setup != GapAndGoSetup.name:
                continue
            try:
                self.executor.close_position(pos.symbol, pos.side, pos.qty)
                closed += 1
            except Exception as exc:
                logger.error("GAPGO_EOD_CLOSE_FAILED symbol=%s error=%s",
                             pos.symbol, exc, exc_info=True)
        self.day.eod_close_done = True
        logger.info("GAPGO_EOD_CLOSE_DONE n=%d", closed)
        return closed

    # ── Day reset ───────────────────────────────────────────────────────

    def reset_for_new_day(self) -> None:
        self.day = _DayState()
        self.scanner.reset_for_new_day()
