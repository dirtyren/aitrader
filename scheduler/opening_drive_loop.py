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

from broker.safe_close import cancel_open_orders_for_symbol
from core.asset_class import AssetClassConfig
from core.bar import Bar
from core.session import SessionContext
from scheduler.loop import record_exits_to_ledger
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
    # Which way the trigger is traded. Detection is identical either way — see
    # OpeningDriveSetup's SIDE note. The live trader leaves this at "long";
    # "short" is used by scripts/backtest_opening_drive.py --side short.
    side: str = "long"


@dataclass
class _DayState:
    cut_done: bool = False
    eod_close_done: bool = False
    # Guards the 16:10 rebuild to once per day. Without it, every re-entry of
    # run_day after the post-close phase re-ran a full 20-session x ~519-symbol
    # bulk fetch AND reset computed_at, which made the staleness check
    # meaningless (it would always look fresh).
    post_close_refresh_done: bool = False
    watchlist: list[ScanResult] = field(default_factory=list)
    setups: dict[str, OpeningDriveSetup] = field(default_factory=dict)
    contexts: dict[str, SessionContext] = field(default_factory=dict)
    # Last managed-phase bar timestamp actually forwarded to PositionManager,
    # per symbol. The managed poll runs every 60s against 5-minute bars, so
    # the same bar arrives ~5 times; forwarding all of them made bars_held
    # count minutes instead of bars (max_hold_bars=36 fired ~11:37, not 14:00).
    last_managed_bar_ts: dict[str, datetime] = field(default_factory=dict)


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
        mysql_store=None,
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
        self.mysql = mysql_store
        self.day = _DayState()

    # ── Time helpers ────────────────────────────────────────────────────

    def or_window(self, day: datetime) -> tuple[datetime, datetime]:
        start = _ny_dt(day, 9, 30)
        return start, start + timedelta(minutes=self.cfg.or_minutes)

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
        # Alpaca's `end` is INCLUSIVE, so a 09:30->10:00 request returns 31
        # bars: the 10:00 bar is the first bar of the entry window, not part
        # of the opening range. Left in, it feeds post-cut information into
        # or_high / or_close (a real lookahead — the range would incorporate
        # a price the strategy has not seen at decision time) and pushes
        # bar_coverage to 31/30 = 1.03. Filter on the timestamp rather than
        # trimming the request, so the invariant holds no matter what the
        # API's boundary semantics do later.
        bars_by_symbol = {
            sym: [b for b in bars if b.ts < or_end]
            for sym, bars in bars_by_symbol.items()
        }
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
                # SPEC MISMATCH (I2, deliberately unresolved): spec 7.2 says
                # the trigger bar's volume is compared against "the trailing
                # bar average", i.e. the recent 1-min bars inside the entry
                # window. This passes the OPENING-RANGE per-minute average
                # (09:30-10:00) instead, which is normally the highest-volume
                # window of the session — so volume_confirm_mult: 2.0 against
                # it is a materially stricter gate than against a 10:00-11:00
                # trailing average, and may suppress otherwise valid triggers.
                # Changing the reference or the multiplier is a tuning
                # decision that needs one paper session of live data to
                # calibrate; it is NOT changed here on speculation.
                avg_minute_volume=r.metrics.or_volume / self.cfg.or_minutes,
                entry_deadline=deadline,
                volume_confirm_mult=self.cfg.volume_confirm_mult,
                target_R=self.cfg.target_R,
                min_stop_atr_frac=self.cfg.min_stop_atr_frac,
                atr_mult_stop_cap=self.cfg.atr_mult_stop_cap,
                side=self.cfg.side,
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
        """Feed ONE new 5-minute bar for ``symbol`` through PositionManager.

        Bars already seen are dropped. The managed-phase poller sleeps 60s and
        hands over ``bars[-1]``, so the same 5-minute bar arrives on roughly
        five consecutive iterations. PositionManager increments ``bars_held``
        once per call, so forwarding every repeat made ``max_hold_bars: 36``
        count MINUTES — the time stop fired around 11:37 instead of the 14:00
        the config documents, capping winners while stops still ran in full.
        Repeats were also ingested into the SessionContext, corrupting
        ctx.atr()/ctx.vwap with duplicate volume.
        """
        last_ts = self.day.last_managed_bar_ts.get(symbol)
        if last_ts is not None and bar.ts <= last_ts:
            return
        self.day.last_managed_bar_ts[symbol] = bar.ts

        ctx = self.day.contexts.get(symbol)
        if ctx is not None:
            ctx.ingest(bar)

        # Snapshot BEFORE PositionManager mutates/closes the book entry —
        # record_exits_to_ledger needs the pre-exit position to compute PnL
        # and R. Same shape as VWAPWaveEngine.tick.
        positions_before = {p.setup: p for p in self.book.get_all(symbol)}
        actions = self.position_manager.on_bar(symbol, bar)
        if not actions:
            return

        # Ledger recording is what makes ConsecutiveLossFilter live: it reads
        # ledger.consec_losses_system, and only DailyLedger.record()
        # increments that. Without this call, consecutive_loss_limit: 2 with
        # loss_filter_scope: system_wide could never fire.
        record_exits_to_ledger(
            self.risk_manager.ledger, symbol, actions, bar,
            mysql_store=self.mysql,
            positions_snapshot=positions_before,
            asset_class="equity",
        )

        # The bracket parent id must be passed: OrderExecutor's time_stop
        # branch cancels open orders for the symbol and falls back to the
        # parent id. Omitting it used to skip the cancel entirely, orphaning
        # the OCO legs with hours of session left.
        parent_order_id = (
            positions_before[actions[0].setup].order_id
            if actions[0].setup in positions_before
            else None
        )
        self.executor.handle_actions(
            actions, asset_class="equity", parent_order_id=parent_order_id,
        )

    # ── Book refresh ────────────────────────────────────────────────────

    def refresh_book_from_mysql(self) -> bool:
        """Rebuild the in-memory book from MySQL. Returns True if it happened.

        MySQL is the source of truth: the reconciler closes rows from broker
        fills, so a book loaded once at boot goes permanently stale. The
        symptom was severe — after the first 15:30 flatten the process still
        believed it held five positions, so ConcurrentPositionFilter rejected
        every subsequent signal and the managed phase submitted market sells
        against yesterday's stops for shares the account no longer held.

        A MySQL failure keeps the existing book rather than emptying it:
        an empty book would let the entry window re-enter symbols already
        held. Same trade-off as main.py's MYSQL_REBUILD_FAILED branch.

        Managed state that MySQL does not round-trip is carried forward — see
        _carry_forward_managed_state. Without that the rebuild silently
        reverted every PositionManager mutation once per minute.
        """
        if self.mysql is None:
            return False
        prior = {
            (p.symbol.replace("/", ""), p.setup): p for p in self.book.all()
        }
        try:
            self.book.replace_from(self.mysql.load_open_positions())
        except Exception as exc:
            logger.error("OD_MYSQL_REBUILD_FAILED: %s", exc, exc_info=True)
            return False
        self._carry_forward_managed_state(prior)
        return True

    def _carry_forward_managed_state(self, prior: dict) -> None:
        """Re-apply in-memory managed state onto the freshly loaded rows.

        ``replace_from`` installs brand-new OpenPosition objects built by
        ``MySQLStore._dict_to_pos`` from the row's columns. Three of the fields
        ``PositionManager._check_position`` mutates are never written back to
        the row by this strategy — ``sync_position_state`` /
        ``update_position_state`` have no caller in main_opening_drive.py or
        this module (main.py:623 is the only call site in the repo) — so a
        plain rebuild resets them once per managed-phase iteration (~60s):

          * ``bars_held`` — written once at entry by ``position_opened`` and
            never again, so the rebuild pinned it at 0. Each iteration then
            incremented it to 1 and the next rebuild reset it, so
            ``max_hold_bars: 36`` could NEVER be reached and the 14:00 time
            stop silently became the 15:30 flatten (~1.5h of extra exposure).
          * ``breakeven_moved`` / ``stop_px`` — a breakeven move made in
            memory was lost, so PositionManager re-emitted ``breakeven`` on
            every later distinct bar and ``_move_equity_stop_to_breakeven``
            kept calling ``replace_order`` on a superseded stop leg.

        ``exit_submitted_at`` is not a column at all;
        ``load_open_positions`` re-seeds it to *now* for rows with
        ``exit_submitted``, which restarted PositionManager's 2h stuck-close
        timeout on every rebuild. Carried forward for the same reason.

        Keyed on the entry ``order_id`` as well as (symbol, setup): a NEW
        position on a symbol previously traded must not inherit the old
        position's bar count (at the 09:00 boot the prior book still holds
        yesterday's flattened rows).
        """
        for pos in self.book.all():
            old = prior.get((pos.symbol.replace("/", ""), pos.setup))
            if old is None or old.order_id != pos.order_id:
                continue
            # max(): if a persisted value is ever higher, trust the row.
            pos.bars_held = max(pos.bars_held, old.bars_held)
            if old.breakeven_moved and not pos.breakeven_moved:
                pos.breakeven_moved = True
                if old.stop_px is not None:
                    pos.stop_px = old.stop_px
            if pos.exit_submitted and old.exit_submitted_at is not None:
                pos.exit_submitted_at = old.exit_submitted_at

    # ── Phase: EOD flat ─────────────────────────────────────────────────

    def force_close_all(self, now: datetime) -> int:
        """Cancel every open order per symbol, then market-close each position.

        Returns the number of positions the broker ACCEPTED a close for.

        Two things this must get right, both learned the hard way:

        1. Cancel ALL open orders for the symbol, not the ids the book carries.
           ``OrderExecutor.submit`` sets ``target_order_id = None``
           unconditionally, so the OCO take-profit sell-limit is invisible to
           the book. Leaving it live holds the shares, Alpaca rejects the
           market close with "insufficient qty available for order", and the
           position is carried overnight with no stop. Enumerating the
           broker's own open orders is the only way to find that leg — the
           same cancel-then-close remediation the reconciler performs.

        2. A ``None`` return from close_position is a FAILURE, not a success.
           ``submit_close_with_drift_recovery`` returns None when both submit
           attempts were rejected. Counting it as closed made the log say
           "closed" for a position still held.

        Cancel failures are tolerated (an already-filled leg raises) and one
        symbol's failure never aborts the sweep.
        """
        if self.day.eod_close_done:
            return 0
        closed = 0
        failed = 0
        for pos in list(self.book.all()):
            if pos.setup != OpeningDriveSetup.name:
                continue
            cancelled = cancel_open_orders_for_symbol(
                self.alpaca, pos.symbol, log=logger, log_prefix="OD_EOD",
            )
            if not cancelled:
                # Fallback only, for when the enumeration itself failed (a 500
                # on list_orders) — cancel whatever ids the book does know.
                # Strictly a subset of the sweep: it cannot see the TP leg.
                self._cancel_known_legs(pos)
            try:
                result = self.executor.close_position(
                    pos.symbol, pos.side, pos.qty,
                    setup=pos.setup, asset_class="equity",
                )
            except Exception as exc:
                failed += 1
                logger.error("OD_EOD_CLOSE_FAILED symbol=%s error=%s",
                             pos.symbol, exc, exc_info=True)
                continue
            if result is None:
                failed += 1
                logger.error(
                    "OD_EOD_CLOSE_REJECTED symbol=%s side=%s qty=%s — broker "
                    "refused the close; the position may be carried overnight "
                    "WITHOUT a stop. Check for live orders holding the qty.",
                    pos.symbol, pos.side, pos.qty,
                )
                continue
            closed += 1
            self._mark_flattened(pos)
        self.day.eod_close_done = True
        logger.info("OD_EOD_CLOSE_DONE closed=%d failed=%d", closed, failed)
        return closed

    def _cancel_known_legs(self, pos) -> None:
        """Cancel the order ids recorded on the book. Best-effort."""
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

    def _mark_flattened(self, pos) -> None:
        """Record that a broker close is in flight for ``pos``.

        exit_submitted is the established contract (OrderExecutor.handle_actions
        uses it): PositionManager then defers everything for this position, so
        the managed phase cannot keep managing a flattened one. The row itself
        is left for the reconciler, which is the writer of record for closes —
        it applies the real fill price and close_reason, and the next
        refresh_book_from_mysql drops the row.

        Without MySQL there is no writer of record, so nothing would ever
        remove the entry; in that case the in-memory book IS the record and we
        close it here.
        """
        try:
            self.executor.mark_exit_submitted(pos.symbol, pos.setup)
        except Exception as exc:
            logger.warning(
                "OD_EOD_MARK_EXIT_FAILED symbol=%s setup=%s error=%s",
                pos.symbol, pos.setup, exc,
            )
        if self.mysql is None:
            self.book.close(pos.symbol, pos.setup)

    # ── Day reset ───────────────────────────────────────────────────────

    def reset_for_new_day(self, now: datetime | None = None) -> None:
        # clear_just_exited matters here: PositionBook.close() (and the no-MySQL
        # flatten path) records the symbol in _just_exited, and
        # OrderExecutor.submit refuses to enter a symbol that is in that set.
        # Nothing else in this strategy clears it — VWAPWaveEngine.tick does it
        # per cycle — so without this, any symbol that stopped out once could
        # never be entered again for the container's lifetime.
        self.book.clear_just_exited()
        self._roll_ledger_day(now or datetime.now(timezone.utc))
        self.day = _DayState()

    def _roll_ledger_day(self, now: datetime) -> None:
        """Reset the per-day ledger counters at the day boundary.

        ``DailyLedger.roll_day`` had no live caller anywhere in production
        (only backtest/intraday_replay.py), and ``consec_losses_system`` is
        cleared ONLY by a recorded win. Now that losing exits actually reach
        the ledger, the counter accumulates across sessions: once it hits
        ``consecutive_loss_limit: 2``, ConsecutiveLossFilter(scope=
        "system_wide") rejects every entry — and with no entries there can be
        no win to clear it. The strategy latched off for the container's
        lifetime behind nothing but a SIGNAL_REJECTED line, the same failure
        class as the _dtbp_exhausted latch.

        Safe here specifically because this is the day boundary
        (main_opening_drive.py calls reset_for_new_day only after run_day's
        tail has slept to the next session's 09:00 boot — a mid-day exception
        retries run_day WITHOUT this reset). roll_day clears day_pnl,
        trades_today and both loss counters but preserves ``equity``, and
        nothing in this strategy reads day_pnl or trades_today across a day.
        """
        ledger = getattr(self.risk_manager, "ledger", None)
        if ledger is None:
            return
        try:
            ledger.roll_day(now)
        except Exception as exc:
            logger.error("OD_LEDGER_ROLL_FAILED: %s", exc, exc_info=True)
            return
        logger.info("OD_LEDGER_DAY_ROLLED at=%s", now.isoformat())
