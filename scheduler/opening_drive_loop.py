"""Opening Drive daily lifecycle orchestrator.

Times below are America/New_York; internal timestamps are timezone-aware UTC.

| Local time  | Action                                                    |
|-------------|-----------------------------------------------------------|
| 16:10 (D-1) | Baseline refresh (post-close, so it never competes)       |
| 09:00       | Boot, validate broker, baseline staleness fallback        |
| 09:30-10:00 | Opening range forms — system idle, no requests issued     |
| 10:00       | Cut: one bulk bars request, gates, rank, build setups     |
| 10:00-11:00 | Entry window: 1-min bars for watchlist symbols only       |
| 11:00       | Window closes; un-triggered setups expire                |
| 11:00-15:30 | Managed phase via PositionManager on 5-min bars           |
| 15:30       | EOD flat — cancel OCO children, then market-close all     |

Unlike gap_and_go there is no snapshot polling loop: the entire opening range
arrives in a single bulk request at 10:00, which is cheaper and immune to the
partial-state bugs that missed polls cause.

Each phase method is independently testable — no sleeps, no network inside
the phase methods themselves.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone

import pytz

from core.asset_class import AssetClassConfig
from core.bar import Bar
from core.session import SessionContext
from state.position_book import PositionBook
from strategies.opening_drive_scanner import OpeningDriveScanner, ScanResult
from strategies.setup_opening_drive import OpeningDriveSetup

logger = logging.getLogger(__name__)

_NY_TZ = pytz.timezone("America/New_York")


def _ny_dt(day: datetime, hh: int, mm: int) -> datetime:
    """Build a timezone-aware UTC datetime from an NY date + local HH:MM."""
    ny_date = day.astimezone(_NY_TZ).date()
    naive = datetime.combine(ny_date, time(hh, mm))
    return _NY_TZ.localize(naive).astimezone(timezone.utc)


@dataclass
class OpeningDriveConfig:
    """The subset of YAML settings this loop consumes."""
    universe_path: str
    baselines_path: str
    baselines_max_age_days: int = 7
    or_minutes: int = 30
    entry_window_minutes: int = 60
    volume_confirm_mult: float = 2.0
    target_R: float = 2.0
    min_stop_atr_frac: float = 0.15
    atr_mult_stop_cap: float = 2.0
    max_concurrent_positions: int = 5
    candidate_multiplier: float = 1.5
    premarket_bar_timeframe: str = "1Min"
    regular_bar_timeframe: str = "5Min"
    lookback_sessions: int = 20


@dataclass
class _DayState:
    cut_done: bool = False
    eod_close_done: bool = False
    watchlist: list[ScanResult] = field(default_factory=list)
    setups: dict[str, OpeningDriveSetup] = field(default_factory=dict)
    contexts: dict[str, SessionContext] = field(default_factory=dict)


class OpeningDriveLoop:
    """Coordinates one trading day for the Opening Drive strategy."""

    def __init__(
        self,
        cfg: OpeningDriveConfig,
        scanner: OpeningDriveScanner,
        equity_asset_class: AssetClassConfig,
        alpaca_client,
        alpaca_data,
        risk_manager,
        executor,
        book: PositionBook,
        position_manager,
        strategy_name: str,
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
        self.strategy_name = strategy_name
        self.day = _DayState()

    # ── Time helpers ────────────────────────────────────────────────────

    def or_window(self, day: datetime) -> tuple[datetime, datetime]:
        """Return (09:30, 10:00) in NY timezone as UTC-aware datetimes.

        The opening range is always the fixed 09:30-10:00 NY clock window.
        cfg.or_minutes controls avg_minute_volume computation only (how many
        bars are expected in the OR window), not the clock-time boundary.
        """
        return _ny_dt(day, 9, 30), _ny_dt(day, 10, 0)

    def cut_time(self, day: datetime) -> datetime:
        return self.or_window(day)[1]

    def entry_window_end(self, day: datetime) -> datetime:
        return self.cut_time(day) + timedelta(
            minutes=self.cfg.entry_window_minutes,
        )

    def eod_close_time(self, day: datetime) -> datetime:
        return _ny_dt(day, 15, 30)

    # ── Prev-close fetch ────────────────────────────────────────────────

    def fetch_prev_closes(
        self, symbols: list[str], now: datetime,
    ) -> dict[str, float]:
        """Prior session's closing price per symbol.

        Fetched fresh at cut time rather than stored on the baseline: a
        skipped baseline refresh would leave a stale prev_close that silently
        corrupts disp_atr and rs_atr.

        Today's partial daily bar must be excluded — a 1Day window touching
        the live session can return one — so bars are filtered to NY dates
        strictly before today's.
        """
        or_start, _ = self.or_window(now)
        today_ny = now.astimezone(_NY_TZ).date()
        bars_by_symbol = self.data.get_bars_multi(
            symbols, "equity", "1Day",
            or_start - timedelta(days=10), or_start,
        )
        out: dict[str, float] = {}
        for sym, bars in bars_by_symbol.items():
            prior = [
                b for b in bars
                if b.ts.astimezone(_NY_TZ).date() < today_ny
            ]
            if prior:
                out[sym] = prior[-1].close
        return out

    # ── Phase: cut ──────────────────────────────────────────────────────

    def run_cut(self, now: datetime) -> list[ScanResult]:
        if self.day.cut_done:
            return self.day.watchlist

        symbols = self.scanner.request_symbols()
        or_start, or_end = self.or_window(now)
        bars_by_symbol = self.data.get_bars_multi(
            symbols, "equity", self.cfg.premarket_bar_timeframe,
            or_start, or_end,
        )
        prev_closes = self.fetch_prev_closes(symbols, now)

        watchlist = self.scanner.run_cut(bars_by_symbol, prev_closes, now)
        self.day.watchlist = watchlist
        deadline = self.entry_window_end(now)

        for r in watchlist:
            or_bars = bars_by_symbol.get(r.symbol) or []
            self.day.setups[r.symbol] = OpeningDriveSetup(
                symbol=r.symbol,
                or_high=r.metrics.or_high,
                or_low=r.metrics.or_low,
                atr_14d=r.metrics.atr_14d,
                avg_minute_volume=r.metrics.or_volume / self.cfg.or_minutes,
                entry_deadline=deadline,
                volume_confirm_mult=self.cfg.volume_confirm_mult,
                target_R=self.cfg.target_R,
                min_stop_atr_frac=self.cfg.min_stop_atr_frac,
                atr_mult_stop_cap=self.cfg.atr_mult_stop_cap,
            )
            # Seed the context with the OR bars so ctx.vwap is SESSION VWAP
            # from 09:30, not the VWAP of post-cut bars only. Without this
            # the setup's VWAP filter is nearly meaningless right after the
            # cut, when the context would hold one or two bars.
            ctx = SessionContext(
                symbol=r.symbol, asset_class=self.equity_asset_class,
            )
            for b in or_bars:
                ctx.ingest(b)
            self.day.contexts[r.symbol] = ctx

        self.day.cut_done = True
        logger.info(
            "OD_CUT_COMPLETE n=%d symbols=%s deadline=%s",
            len(watchlist), [r.symbol for r in watchlist],
            deadline.isoformat(),
        )
        return watchlist

    # ── Phase: entry window ─────────────────────────────────────────────

    def on_bar(self, symbol: str, bar: Bar) -> None:
        """Push one closed 1-min bar through the symbol's setup."""
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
            logger.info("OD_REJECTED symbol=%s reason=%s",
                        symbol, decision.reason)
            return
        logger.info(
            "OD_SIGNAL_FIRED symbol=%s entry=%.4f stop=%.4f target=%.4f",
            symbol, signal.entry, signal.stop, signal.target,
        )
        self.executor.submit(signal, decision, asset_class="equity")

    # ── Phase: timeframe boundary reset ────────────────────────────────

    def switch_to_regular_session_bars(self) -> None:
        """Reset each watchlist symbol's SessionContext at the 11:00 boundary.

        The entry-window context held 1-min bars; the managed phase ingests
        5-min bars. Mixing bar timeframes in one context corrupts ctx.atr()
        and ctx.vwap — the same problem gap_and_go_loop solves with its own
        switch_to_regular_session_bars at the 09:30 boundary.

        This must be called once before the managed phase begins, before any
        5-min bars are passed to manage_open.
        """
        for sym in list(self.day.contexts.keys()):
            self.day.contexts[sym] = SessionContext(
                symbol=sym, asset_class=self.equity_asset_class,
            )

    # ── Phase: managed ──────────────────────────────────────────────────

    def manage_open(self, symbol: str, bar: Bar) -> None:
        ctx = self.day.contexts.get(symbol)
        if ctx is not None:
            ctx.ingest(bar)
        actions = self.position_manager.on_bar(symbol, bar)
        if actions:
            self.executor.handle_actions(actions, asset_class="equity")

    # ── Phase: EOD flat ─────────────────────────────────────────────────

    def force_close_all(self, now: datetime) -> int:
        """Cancel OCO children, then market-close every Opening Drive position.

        Cancelling FIRST is required: OrderExecutor.close_position submits a
        market close but does not touch the bracket legs, so flattening with
        live stop/target orders leaves orphaned orders behind — the failure
        class the reconciler exists to clean up.

        Cancel failures are tolerated: an already-filled or already-cancelled
        leg raises, and that must not prevent the close.
        """
        if self.day.eod_close_done:
            return 0
        closed = 0
        for pos in list(self.book.all()):
            if pos.setup != OpeningDriveSetup.name:
                continue
            for oid in (pos.stop_order_id, pos.target_order_id):
                if not oid:
                    continue
                try:
                    self.alpaca.cancel_order(oid)
                except Exception as exc:
                    logger.warning(
                        "OD_EOD_CANCEL_FAILED symbol=%s order_id=%s error=%s",
                        pos.symbol, oid, exc,
                    )
            try:
                self.executor.close_position(
                    pos.symbol, pos.side, pos.qty,
                    setup=pos.setup, asset_class="equity",
                )
                closed += 1
            except Exception as exc:
                logger.error("OD_EOD_CLOSE_FAILED symbol=%s error=%s",
                             pos.symbol, exc, exc_info=True)
        self.day.eod_close_done = True
        logger.info("OD_EOD_CLOSE_DONE n=%d", closed)
        return closed

    # ── Day reset ───────────────────────────────────────────────────────

    def reset_for_new_day(self) -> None:
        self.day = _DayState()
