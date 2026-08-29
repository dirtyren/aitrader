# tests/test_opening_drive_loop.py
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from core.asset_class import AssetClassConfig
from core.bar import Bar
from core.position_manager import PositionAction
from risk.manager import RiskDecision
from scheduler.opening_drive_loop import OpeningDriveConfig, OpeningDriveLoop
from state.daily_ledger import DailyLedger
from state.position_book import OpenPosition, PositionBook
from strategies.opening_drive_scanner import (
    OpeningDriveBaseline, OpeningDriveScanner,
)
from strategies.setup_opening_drive import OpeningDriveSetup

EQUITY_AC = AssetClassConfig(
    name="equity", timezone="America/New_York",
    session_open_local="09:30", session_close_local="16:00",
    opening_blackout_min=0, bar_timeframe="1Min",
    slippage_bps=2.0, commission_per_share=0.0, commission_bps=0.0,
)

DAY = datetime(2026, 8, 28, 14, 0, tzinfo=timezone.utc)   # 10:00 NY
NOW = DAY


def _baseline(**kw) -> OpeningDriveBaseline:
    return OpeningDriveBaseline(
        atr_14d=kw.get("atr_14d", 4.0),
        avg_or_volume_20d=kw.get("avg_or_volume_20d", 10_000.0),
        avg_daily_volume_20d=kw.get("avg_daily_volume_20d", 400_000.0),
        computed_at=NOW - timedelta(days=1),
    )


def _or_bars(symbol: str, close: float, volume: float = 30_000.0) -> list[Bar]:
    base = datetime(2026, 8, 28, 13, 30, tzinfo=timezone.utc)
    return [
        Bar(symbol=symbol, ts=base, open=100.0, high=102.0, low=99.0,
            close=101.0, volume=volume / 3),
        Bar(symbol=symbol, ts=base + timedelta(minutes=1), open=101.0,
            high=max(105.0, close + 1), low=100.5, close=103.0,
            volume=volume / 3),
        Bar(symbol=symbol, ts=base + timedelta(minutes=2), open=103.0,
            high=max(105.0, close + 1), low=min(102.0, close), close=close,
            volume=volume / 3),
    ]


def _build(**kw):
    universe = kw.get("universe", {"AAA": "Tech", "BBB": "Energy"})
    or_minutes = kw.get("or_minutes", 3)
    baselines = {s: _baseline() for s in list(universe) + ["SPY"]}
    scanner = OpeningDriveScanner(
        universe=universe, baselines=baselines,
        max_concurrent_positions=2, or_minutes=or_minutes,
    )
    cfg = OpeningDriveConfig(
        universe_path="config/universe_sp500_ndx100.csv",
        baselines_path="runtime/opening_drive/baselines.json",
        or_minutes=or_minutes, max_concurrent_positions=2,
    )
    alpaca, data = MagicMock(), MagicMock()
    rm, ex = MagicMock(), MagicMock()
    # position_manager/book are injectable so a test can combine a REAL
    # PositionManager with a REAL rebuild (see the max_hold_bars test): the
    # MagicMock default is exactly what hid the bars_held reset.
    pm = kw.get("position_manager")
    book = kw.get("book") or PositionBook()
    if pm is None:
        pm = MagicMock()
        pm.on_bar.return_value = []
    rm.evaluate.return_value = RiskDecision(approved=True, qty=10, notional=1000)
    # A REAL ledger: ConsecutiveLossFilter reads ledger.consec_losses_system,
    # which only DailyLedger.record() increments (I4).
    rm.ledger = kw.get("ledger", DailyLedger(initial_equity=100_000.0))
    # close_position returns the broker order dict on success; None is a
    # FAILED close (submit_close_with_drift_recovery's failure return).
    ex.close_position.return_value = {"id": "close-1"}
    # Broker-side open orders for the cancel-all sweep; overridden per test.
    alpaca.list_orders.return_value = []

    or_bars = kw.get("or_bars", {
        "AAA": _or_bars("AAA", 104.0, 60_000),
        "BBB": _or_bars("BBB", 104.0, 30_000),
        "SPY": _or_bars("SPY", 100.5, 30_000),
    })
    prev_closes = kw.get("prev_closes",
                         {"AAA": 100.0, "BBB": 100.0, "SPY": 100.0})
    data.get_bars_multi.return_value = or_bars

    loop = OpeningDriveLoop(
        cfg=cfg, scanner=scanner, equity_asset_class=EQUITY_AC,
        alpaca_client=alpaca, alpaca_data=data, risk_manager=rm,
        executor=ex, book=book, position_manager=pm,
        strategy_name="opening_drive_equity_trader",
        mysql_store=kw.get("mysql", MagicMock()),
    )
    loop.fetch_prev_closes = MagicMock(return_value=prev_closes)
    return loop, alpaca, ex, rm, book, pm, data


# ── time helpers ───────────────────────────────────────────────────────

def test_or_window_spans_or_minutes_from_0930():
    """or_window is parameterised: start always 09:30 NY, end = start + or_minutes."""
    loop, *_ = _build()   # or_minutes=3 by default in _build()
    start, end = loop.or_window(DAY)
    assert start == datetime(2026, 8, 28, 13, 30, tzinfo=timezone.utc)   # 09:30 NY
    assert end == datetime(2026, 8, 28, 13, 33, tzinfo=timezone.utc)     # 09:33 NY (09:30 + 3 min)


def test_cut_and_window_and_eod_times():
    """Time helpers are self-consistent with the configured or_minutes=3."""
    loop, *_ = _build()   # or_minutes=3
    assert loop.cut_time(DAY) == datetime(2026, 8, 28, 13, 33, tzinfo=timezone.utc)      # 09:33 NY
    assert loop.entry_window_end(DAY) == datetime(2026, 8, 28, 14, 33, tzinfo=timezone.utc)  # 10:33 NY
    assert loop.eod_close_time(DAY) == datetime(2026, 8, 28, 19, 30, tzinfo=timezone.utc)    # 15:30 NY


def test_production_time_helpers_or_minutes_30():
    """Pin the production time values.

    In the deployed config or_minutes=30 yields the expected 09:30-10:00
    opening range, 10:00 cut, 11:00 entry-window end, and 15:30 EOD close.
    This is the case that actually ships; the rest of the test suite uses
    or_minutes=3 to keep fixtures small.
    """
    loop, *_ = _build(or_minutes=30)
    assert loop.or_window(DAY) == (
        datetime(2026, 8, 28, 13, 30, tzinfo=timezone.utc),   # 09:30 NY
        datetime(2026, 8, 28, 14, 0, tzinfo=timezone.utc),    # 10:00 NY
    )
    assert loop.cut_time(DAY) == datetime(2026, 8, 28, 14, 0, tzinfo=timezone.utc)    # 10:00 NY
    assert loop.entry_window_end(DAY) == datetime(2026, 8, 28, 15, 0, tzinfo=timezone.utc)  # 11:00 NY
    assert loop.eod_close_time(DAY) == datetime(2026, 8, 28, 19, 30, tzinfo=timezone.utc)   # 15:30 NY


# ── cut ────────────────────────────────────────────────────────────────

def test_run_cut_requests_universe_plus_spy():
    loop, _, _, _, _, _, data = _build()
    loop.run_cut(NOW)
    requested = data.get_bars_multi.call_args.args[0]
    assert "SPY" in requested
    assert set(requested) >= {"AAA", "BBB", "SPY"}


def test_run_cut_builds_setups_and_contexts():
    loop, *_ = _build()
    watchlist = loop.run_cut(NOW)
    assert [r.symbol for r in watchlist] == ["AAA", "BBB"]
    assert set(loop.day.setups) == {"AAA", "BBB"}
    assert set(loop.day.contexts) == {"AAA", "BBB"}
    assert isinstance(loop.day.setups["AAA"], OpeningDriveSetup)


def test_contexts_are_seeded_with_or_bars_so_vwap_is_session_vwap():
    """Unseeded contexts would compute VWAP from post-cut bars only, making
    the setup's VWAP filter nearly meaningless right after the cut."""
    loop, *_ = _build()
    loop.run_cut(NOW)
    ctx = loop.day.contexts["AAA"]
    assert ctx.bar_count == 3
    expected = (
        sum(b.typical_price * b.volume for b in _or_bars("AAA", 104.0, 60_000))
        / 60_000
    )
    assert ctx.vwap == pytest.approx(expected)


def test_setup_avg_minute_volume_is_or_volume_over_or_minutes():
    loop, *_ = _build()
    loop.run_cut(NOW)
    assert loop.day.setups["AAA"].avg_minute_volume == pytest.approx(60_000 / 3)


def test_setup_deadline_is_entry_window_end():
    loop, *_ = _build()
    loop.run_cut(NOW)
    assert loop.day.setups["AAA"].entry_deadline == loop.entry_window_end(NOW)


def test_run_cut_is_idempotent():
    loop, _, _, _, _, _, data = _build()
    first = loop.run_cut(NOW)
    second = loop.run_cut(NOW)
    assert first == second
    assert data.get_bars_multi.call_count == 1


# ── fetch_prev_closes (real implementation) ────────────────────────────
#
# _build() monkey-patches fetch_prev_closes so the cut tests stay network-
# free.  These tests call OpeningDriveLoop.fetch_prev_closes(loop, ...) —
# the unbound class method — which bypasses the instance mock and exercises
# the real filtering logic.

def _daily_bar(symbol: str, ts: datetime, close: float) -> Bar:
    """Minimal valid daily bar: OHLC all equal to close."""
    return Bar(symbol=symbol, ts=ts, open=close, high=close, low=close,
               close=close, volume=100_000)


def test_fetch_prev_closes_excludes_today_bar():
    """Returns prior[-1].close, not bars[-1].close.

    The broker can return a partial bar for today as the last element.
    The two code paths give numerically different answers here (100.0 vs
    999.0), so a regression to bars[-1] is immediately visible.
    """
    loop, _, _, _, _, _, data = _build()
    today_ts = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)  # 08:00 NY today
    yest_ts  = datetime(2026, 8, 27, 20, 0, tzinfo=timezone.utc)  # 16:00 NY yesterday
    data.get_bars_multi.return_value = {
        "AAA": [
            _daily_bar("AAA", yest_ts,  close=100.0),   # yesterday — correct answer
            _daily_bar("AAA", today_ts, close=999.0),   # today partial — must be excluded
        ],
    }
    result = OpeningDriveLoop.fetch_prev_closes(loop, ["AAA"], NOW)
    assert result["AAA"] == pytest.approx(100.0)


def test_fetch_prev_closes_symbol_with_only_today_bar_is_absent():
    """A symbol whose only broker bar is today's partial bar is absent."""
    loop, _, _, _, _, _, data = _build()
    today_ts = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
    data.get_bars_multi.return_value = {
        "AAA": [_daily_bar("AAA", today_ts, close=105.0)],
    }
    result = OpeningDriveLoop.fetch_prev_closes(loop, ["AAA"], NOW)
    assert "AAA" not in result


def test_fetch_prev_closes_absent_symbol_is_absent():
    """A symbol not present in the broker response is absent from the result."""
    loop, _, _, _, _, _, data = _build()
    data.get_bars_multi.return_value = {}
    result = OpeningDriveLoop.fetch_prev_closes(loop, ["AAA"], NOW)
    assert "AAA" not in result


def test_fetch_prev_closes_requests_1day_timeframe():
    """The third positional arg to get_bars_multi must be '1Day'."""
    loop, _, _, _, _, _, data = _build()
    data.get_bars_multi.return_value = {}
    OpeningDriveLoop.fetch_prev_closes(loop, ["AAA"], NOW)
    assert data.get_bars_multi.call_args.args[2] == "1Day"


def test_fetch_prev_closes_uses_ny_date_not_utc_date():
    """A bar at 03:00 UTC on 2026-08-28 is 23:00 EDT on 2026-08-27.

    Its NY date is 2026-08-27 (yesterday), so it is a valid prior-day bar
    and MUST be included.  A filter that uses b.ts.date() (UTC) would see
    the UTC date 2026-08-28 == today and incorrectly exclude the bar,
    leaving the symbol absent.  This case separates the correct NY-date
    filter from a wrong UTC-date filter.
    """
    loop, _, _, _, _, _, data = _build()
    # 2026-08-28 03:00 UTC  ==  2026-08-27 23:00 EDT  (NY date: yesterday)
    boundary_ts = datetime(2026, 8, 28, 3, 0, tzinfo=timezone.utc)
    data.get_bars_multi.return_value = {
        "AAA": [_daily_bar("AAA", boundary_ts, close=95.0)],
    }
    result = OpeningDriveLoop.fetch_prev_closes(loop, ["AAA"], NOW)
    assert result.get("AAA") == pytest.approx(95.0)


# ── entry ──────────────────────────────────────────────────────────────

def _reclaim_bar(symbol: str, minute: int = 1) -> Bar:
    return Bar(symbol=symbol, ts=DAY + timedelta(minutes=minute),
               open=104.0, high=107.0, low=103.5, close=106.0, volume=90_000)


def test_on_bar_submits_when_trigger_and_risk_approve():
    loop, _, ex, rm, *_ = _build()
    loop.run_cut(NOW)
    loop.on_bar("AAA", _reclaim_bar("AAA"))
    signal_arg = rm.evaluate.call_args.args[0]
    assert signal_arg.symbol == "AAA"
    assert signal_arg.side == "long"
    assert rm.evaluate.call_args.args[2] == "equity"
    assert ex.submit.called
    assert ex.submit.call_args.kwargs["asset_class"] == "equity"


def test_on_bar_does_not_submit_when_risk_rejects():
    loop, _, ex, rm, *_ = _build()
    rm.evaluate.return_value = RiskDecision.reject("sector_exposure: full")
    loop.run_cut(NOW)
    loop.on_bar("AAA", _reclaim_bar("AAA"))
    assert not ex.submit.called


def test_slots_are_first_come_first_served_not_rank_reserved():
    """Spec 7.1: a LOWER-ranked symbol that triggers first takes the slot.
    Nothing reserves capacity for higher-ranked candidates -- trigger timing
    is itself treated as information. Pinning this so the behaviour is a
    decision rather than an accident of arrival order."""
    loop, _, ex, _, _, _, _ = _build()
    watchlist = loop.run_cut(NOW)
    assert watchlist[0].symbol == "AAA"        # AAA outranks BBB on rvol
    loop.on_bar("BBB", _reclaim_bar("BBB"))    # but BBB triggers first
    assert ex.submit.called
    assert ex.submit.call_args.args[0].symbol == "BBB"


def test_on_bar_ignores_symbols_not_on_the_watchlist():
    loop, _, ex, rm, *_ = _build()
    loop.run_cut(NOW)
    loop.on_bar("ZZZ", _reclaim_bar("ZZZ"))
    assert not rm.evaluate.called
    assert not ex.submit.called


def test_on_bar_before_cut_is_a_noop():
    loop, _, ex, rm, *_ = _build()
    loop.on_bar("AAA", _reclaim_bar("AAA"))
    assert not ex.submit.called


# ── timeframe boundary reset ────────────────────────────────────────────

def test_switch_to_regular_session_bars_resets_contexts():
    """After switch_to_regular_session_bars each watchlist context is empty
    (bar_count == 0) while the setups dict is untouched.

    Mixing 1-min entry-window bars with 5-min managed-phase bars in one
    SessionContext corrupts ctx.atr() and ctx.vwap -- this reset is the
    boundary that prevents the corruption."""
    loop, *_ = _build()
    loop.run_cut(NOW)
    # Contexts are seeded with 3 OR bars at cut time
    assert loop.day.contexts["AAA"].bar_count == 3
    assert loop.day.contexts["BBB"].bar_count == 3
    loop.switch_to_regular_session_bars()
    assert loop.day.contexts["AAA"].bar_count == 0
    assert loop.day.contexts["BBB"].bar_count == 0
    # Setups are untouched -- entries can still fire until the deadline
    assert set(loop.day.setups) == {"AAA", "BBB"}


# ── managed phase ──────────────────────────────────────────────────────

def _pa(kind, symbol="AAA", setup="opening_drive", price=100.0, qty=10,
        side="long") -> PositionAction:
    return PositionAction(symbol=symbol, setup=setup, kind=kind, price=price,
                          qty=qty, side=side)


def test_manage_open_routes_position_manager_actions():
    loop, _, ex, _, book, pm, _ = _build()
    loop.run_cut(NOW)
    book.add(_open_pos())
    pm.on_bar.return_value = [_pa("time_stop", price=101.0)]
    loop.manage_open("AAA", _reclaim_bar("AAA", minute=90))
    ex.handle_actions.assert_called_once()
    assert ex.handle_actions.call_args.kwargs["asset_class"] == "equity"


def test_manage_open_with_no_actions_does_not_call_executor():
    loop, _, ex, _, _, pm, _ = _build()
    loop.run_cut(NOW)
    pm.on_bar.return_value = []
    loop.manage_open("AAA", _reclaim_bar("AAA", minute=90))
    assert not ex.handle_actions.called


def test_manage_open_passes_bracket_parent_order_id_to_handle_actions():
    """I1: without a parent id, OrderExecutor's time_stop branch skipped its
    cancel entirely, so every time-stopped position left live OCO legs."""
    loop, _, ex, _, book, pm, _ = _build()
    loop.run_cut(NOW)
    book.add(_open_pos())            # order_id == "o-AAA"
    pm.on_bar.return_value = [_pa("time_stop", price=101.0)]
    loop.manage_open("AAA", _reclaim_bar("AAA", minute=90))
    assert ex.handle_actions.call_args.kwargs["parent_order_id"] == "o-AAA"


def test_manage_open_ignores_a_bar_it_has_already_seen():
    """I1 — the discriminating case.

    The managed-phase poll runs every 60s but the bars are 5-minute, so the
    SAME bar is handed to manage_open on ~5 consecutive iterations. The
    pre-fix code forwarded every one to PositionManager.on_bar, which
    increments bars_held per call — so max_hold_bars=36 counted MINUTES and
    fired around 11:37 instead of 14:00.
    """
    loop, _, _, _, _, pm, _ = _build()
    loop.run_cut(NOW)
    bar = _reclaim_bar("AAA", minute=90)
    for _ in range(5):
        loop.manage_open("AAA", bar)
    assert pm.on_bar.call_count == 1, (
        "the same 5-minute bar reached PositionManager more than once — "
        "bars_held would advance once per MINUTE, not once per bar"
    )


def test_manage_open_forwards_each_distinct_bar_exactly_once():
    """Three distinct 5-minute bars must advance bars_held three times; a
    repeated or out-of-order bar in between must not."""
    loop, _, _, _, _, pm, _ = _build()
    loop.run_cut(NOW)
    b1 = _reclaim_bar("AAA", minute=90)
    b2 = _reclaim_bar("AAA", minute=95)
    b3 = _reclaim_bar("AAA", minute=100)
    for bar in (b1, b1, b2, b1, b2, b3, b3):
        loop.manage_open("AAA", bar)
    forwarded = [c.args[1].ts for c in pm.on_bar.call_args_list]
    assert forwarded == [b1.ts, b2.ts, b3.ts]


def test_manage_open_tracks_last_bar_per_symbol_independently():
    loop, _, _, _, _, pm, _ = _build()
    loop.run_cut(NOW)
    loop.manage_open("AAA", _reclaim_bar("AAA", minute=90))
    loop.manage_open("BBB", _reclaim_bar("BBB", minute=90))   # same ts
    assert [c.args[0] for c in pm.on_bar.call_args_list] == ["AAA", "BBB"]


def test_manage_open_does_not_double_ingest_a_repeated_bar_into_the_context():
    loop, *_ = _build()
    loop.run_cut(NOW)
    loop.switch_to_regular_session_bars()
    bar = _reclaim_bar("AAA", minute=90)
    for _ in range(4):
        loop.manage_open("AAA", bar)
    assert loop.day.contexts["AAA"].bar_count == 1


def test_reset_for_new_day_clears_the_seen_bar_cursor():
    """Yesterday's cursor must not suppress today's first managed bar."""
    loop, _, _, _, _, pm, _ = _build()
    loop.run_cut(NOW)
    bar = _reclaim_bar("AAA", minute=90)
    loop.manage_open("AAA", bar)
    loop.reset_for_new_day()
    loop.run_cut(NOW)
    loop.manage_open("AAA", bar)
    assert pm.on_bar.call_count == 2


# ── managed phase: ledger wiring (I4) ──────────────────────────────────

def test_manage_open_records_a_losing_exit_into_the_ledger():
    """I4: ConsecutiveLossFilter reads ledger.consec_losses_system, which only
    DailyLedger.record() increments. Nothing in this strategy called it, so
    consecutive_loss_limit=2 with scope system_wide could never fire."""
    ledger = DailyLedger(initial_equity=100_000.0)
    loop, _, _, _, book, pm, _ = _build(ledger=ledger)
    loop.run_cut(NOW)
    book.add(_open_pos())                       # entry 100.0, stop 98.0
    pm.on_bar.return_value = [_pa("stop", price=98.0)]
    loop.manage_open("AAA", _reclaim_bar("AAA", minute=90))
    assert len(ledger.trades_today) == 1
    rec = ledger.trades_today[0]
    assert rec.symbol == "AAA"
    assert rec.setup == "opening_drive"
    assert rec.exit_px == pytest.approx(98.0)
    assert rec.pnl_usd == pytest.approx((98.0 - 100.0) * 10)
    assert rec.R_realized == pytest.approx(-1.0)
    assert ledger.consec_losses_system == 1


def test_two_losing_exits_arm_the_consecutive_loss_brake():
    """The brake must actually engage: two recorded losses must make
    ConsecutiveLossFilter(limit=2, scope=system_wide) reject."""
    from risk.filters import ConsecutiveLossFilter

    ledger = DailyLedger(initial_equity=100_000.0)
    loop, _, _, _, book, pm, _ = _build(ledger=ledger)
    loop.run_cut(NOW)
    for i, sym in enumerate(("AAA", "BBB")):
        book.add(_open_pos(symbol=sym))
        pm.on_bar.return_value = [_pa("stop", symbol=sym, price=98.0)]
        loop.manage_open(sym, _reclaim_bar(sym, minute=90 + i))
    assert ledger.consec_losses_system == 2
    signal = MagicMock()
    signal.symbol = "CCC"
    verdict = ConsecutiveLossFilter(limit=2, scope="system_wide").check(
        signal, None, ledger, book,
    )
    assert verdict.passed is False


def test_manage_open_records_a_winning_exit_and_clears_the_streak():
    ledger = DailyLedger(initial_equity=100_000.0)
    ledger.consec_losses_system = 2
    loop, _, _, _, book, pm, _ = _build(ledger=ledger)
    loop.run_cut(NOW)
    book.add(_open_pos())
    pm.on_bar.return_value = [_pa("target", price=104.0)]
    loop.manage_open("AAA", _reclaim_bar("AAA", minute=90))
    assert ledger.consec_losses_system == 0
    assert ledger.trades_today[0].pnl_usd == pytest.approx(40.0)


def test_manage_open_does_not_record_a_breakeven_action():
    ledger = DailyLedger(initial_equity=100_000.0)
    loop, _, ex, _, book, pm, _ = _build(ledger=ledger)
    loop.run_cut(NOW)
    book.add(_open_pos())
    pm.on_bar.return_value = [_pa("breakeven", price=100.0)]
    loop.manage_open("AAA", _reclaim_bar("AAA", minute=90))
    assert ledger.trades_today == []
    assert ex.handle_actions.called      # still routed to the broker


# ── managed phase: book refresh (C2) ───────────────────────────────────

def test_refresh_book_from_mysql_replaces_stale_entries():
    """C2: the book was loaded once at boot and never rebuilt, so after the
    first 15:30 flatten the process believed it still held its positions
    forever — ConcurrentPositionFilter then rejected every later signal and
    the managed phase sold shares the account no longer held."""
    mysql = MagicMock()
    loop, _, _, _, book, _, _ = _build(mysql=mysql)
    book.add(_open_pos(symbol="AAA"))
    fresh = PositionBook()
    fresh.add(_open_pos(symbol="ZZZ"))
    mysql.load_open_positions.return_value = fresh
    assert loop.refresh_book_from_mysql() is True
    assert [p.symbol for p in book.all()] == ["ZZZ"]


def test_refresh_book_from_mysql_is_a_noop_without_persistence():
    loop, _, _, _, book, _, _ = _build(mysql=None)
    book.add(_open_pos(symbol="AAA"))
    assert loop.refresh_book_from_mysql() is False
    assert [p.symbol for p in book.all()] == ["AAA"]


def test_refresh_book_from_mysql_keeps_the_old_book_on_error():
    """A MySQL blip must not empty the book — that would let the entry window
    re-enter symbols the strategy already holds."""
    mysql = MagicMock()
    mysql.load_open_positions.side_effect = RuntimeError("gone away")
    loop, _, _, _, book, _, _ = _build(mysql=mysql)
    book.add(_open_pos(symbol="AAA"))
    assert loop.refresh_book_from_mysql() is False
    assert [p.symbol for p in book.all()] == ["AAA"]


# ── EOD flatten ────────────────────────────────────────────────────────

def _open_pos(symbol="AAA", setup="opening_drive", **kw) -> OpenPosition:
    """A position shaped the way the state layer ACTUALLY produces it.

    ``target_order_id`` defaults to None because OrderExecutor.submit sets it
    to None unconditionally for every asset class — the OCO take-profit leg
    exists at the broker but is never recorded on the book. The old fixture
    hardcoded "tgt-1", an impossible state, which is exactly why the
    cancel-the-book's-ids implementation passed its test while leaving the
    live sell-limit holding the shares.
    """
    return OpenPosition(
        symbol=symbol, setup=setup, side="long", qty=10, entry_px=100.0,
        stop_px=98.0, target_px=104.0, opened_at=NOW, order_id=f"o-{symbol}",
        stop_order_id=kw.get("stop_order_id", "stop-1"),
        target_order_id=kw.get("target_order_id", None),
    )


def _broker_open_orders(alpaca, *order_ids):
    alpaca.list_orders.return_value = [{"id": oid} for oid in order_ids]


def test_force_close_cancels_the_take_profit_leg_the_book_never_recorded():
    """C1 — the discriminating case.

    The broker holds BOTH OCO legs, but the book only knows stop-1
    (target_order_id is always None). Cancelling only the book's ids leaves
    the sell-limit live, it holds the shares, and Alpaca rejects the market
    close with "insufficient qty available for order".
    """
    loop, alpaca, ex, _, book, _, _ = _build()
    book.add(_open_pos())
    _broker_open_orders(alpaca, "stop-1", "tp-1")
    calls: list[str] = []
    alpaca.cancel_order.side_effect = lambda oid: calls.append(f"cancel:{oid}")
    ex.close_position.side_effect = lambda *a, **k: (
        calls.append("close") or {"id": "close-1"}
    )

    assert loop.force_close_all(NOW) == 1

    alpaca.list_orders.assert_called_once_with(
        status="open", symbols=["AAA"], nested=False,
    )
    assert calls.index("cancel:stop-1") < calls.index("close")
    assert calls.index("cancel:tp-1") < calls.index("close"), (
        "the OCO take-profit leg was never cancelled — it is not on the book, "
        "so only enumerating the broker's open orders can find it"
    )


def test_force_close_still_closes_when_cancel_fails():
    """An already-filled leg raises on cancel; the close must still happen."""
    loop, alpaca, ex, _, book, _, _ = _build()
    book.add(_open_pos())
    _broker_open_orders(alpaca, "stop-1", "tp-1")
    alpaca.cancel_order.side_effect = RuntimeError("order not cancelable")
    assert loop.force_close_all(NOW) == 1
    assert ex.close_position.called


def test_force_close_still_closes_when_list_orders_fails():
    loop, alpaca, ex, _, book, _, _ = _build()
    book.add(_open_pos())
    alpaca.list_orders.side_effect = RuntimeError("alpaca 500")
    assert loop.force_close_all(NOW) == 1
    assert ex.close_position.called


def test_force_close_falls_back_to_the_books_ids_when_the_sweep_fails():
    """A 500 on list_orders must not mean cancelling nothing at all."""
    loop, alpaca, ex, _, book, _, _ = _build()
    book.add(_open_pos(stop_order_id="stop-1"))
    alpaca.list_orders.side_effect = RuntimeError("alpaca 500")
    loop.force_close_all(NOW)
    assert [c.args[0] for c in alpaca.cancel_order.call_args_list] == ["stop-1"]


def test_force_close_does_not_double_cancel_when_the_sweep_worked():
    loop, alpaca, ex, _, book, _, _ = _build()
    book.add(_open_pos(stop_order_id="stop-1"))
    _broker_open_orders(alpaca, "stop-1", "tp-1")
    loop.force_close_all(NOW)
    assert [c.args[0] for c in alpaca.cancel_order.call_args_list] == [
        "stop-1", "tp-1",
    ]


def test_force_close_does_not_count_a_rejected_close_as_closed():
    """C1 second half — the discriminating case.

    submit_close_with_drift_recovery returns None when the close was
    REJECTED (e.g. the TP leg still holds the qty). The pre-fix code
    incremented `closed` anyway and logged success, so the operator saw
    "OD_EOD_CLOSE_DONE n=1" for a position carried overnight with no stop.
    """
    loop, alpaca, ex, _, book, _, _ = _build()
    book.add(_open_pos())
    ex.close_position.return_value = None
    assert loop.force_close_all(NOW) == 0, (
        "a None return from close_position is a FAILED close and must not be "
        "counted as closed"
    )
    assert not ex.mark_exit_submitted.called


def test_force_close_logs_a_rejected_close_at_error_level(caplog):
    loop, _, ex, _, book, _, _ = _build()
    book.add(_open_pos())
    ex.close_position.return_value = None
    with caplog.at_level("ERROR", logger="scheduler.opening_drive_loop"):
        loop.force_close_all(NOW)
    assert any(r.levelname == "ERROR" and "AAA" in r.getMessage()
               for r in caplog.records)


def test_force_close_summary_distinguishes_closed_from_failed(caplog):
    loop, _, ex, _, book, _, _ = _build()
    book.add(_open_pos(symbol="AAA"))
    book.add(_open_pos(symbol="BBB"))
    ex.close_position.side_effect = [None, {"id": "close-1"}]
    with caplog.at_level("INFO", logger="scheduler.opening_drive_loop"):
        assert loop.force_close_all(NOW) == 1
    summary = [r.getMessage() for r in caplog.records
               if "OD_EOD_CLOSE_DONE" in r.getMessage()]
    assert summary and "closed=1" in summary[0] and "failed=1" in summary[0]


def test_force_close_marks_exit_submitted_so_the_book_stops_acting_on_it():
    """C2: the flatten submitted broker closes but never marked the book, so
    the managed phase kept managing flattened positions."""
    loop, _, ex, _, book, _, _ = _build()
    book.add(_open_pos())
    loop.force_close_all(NOW)
    ex.mark_exit_submitted.assert_called_once_with("AAA", "opening_drive")


def test_force_close_drops_the_book_entry_when_there_is_no_mysql():
    """With no MySQL there is no writer of record for closes, so nothing
    would ever remove the row — the in-memory book is the only record."""
    loop, _, _, _, book, _, _ = _build(mysql=None)
    book.add(_open_pos())
    loop.force_close_all(NOW)
    assert book.count() == 0


def test_force_close_leaves_the_row_for_the_reconciler_when_mysql_is_present():
    """The reconciler is the writer of record: it closes the MySQL row from
    the broker fill and the next book refresh drops it. Deleting the row here
    would race that and lose the realised fill."""
    loop, _, _, _, book, _, _ = _build(mysql=MagicMock())
    book.add(_open_pos())
    loop.force_close_all(NOW)
    assert book.count() == 1


def test_force_close_ignores_other_strategies_positions():
    loop, alpaca, ex, _, book, _, _ = _build()
    book.add(_open_pos(symbol="TQQQ", setup="sma_slope"))
    assert loop.force_close_all(NOW) == 0
    assert not ex.close_position.called
    assert not alpaca.list_orders.called


def test_force_close_handles_positions_without_oco_ids():
    loop, alpaca, ex, _, book, _, _ = _build()
    book.add(_open_pos(stop_order_id=None, target_order_id=None))
    _broker_open_orders(alpaca)          # broker reports nothing open
    assert loop.force_close_all(NOW) == 1
    assert not alpaca.cancel_order.called
    assert ex.close_position.called


def test_force_close_is_idempotent():
    loop, _, ex, _, book, _, _ = _build()
    book.add(_open_pos())
    loop.force_close_all(NOW)
    assert loop.force_close_all(NOW) == 0
    assert ex.close_position.call_count == 1


def test_force_close_continues_after_one_symbol_fails():
    loop, _, ex, _, book, _, _ = _build()
    book.add(_open_pos(symbol="AAA"))
    book.add(_open_pos(symbol="BBB"))
    ex.close_position.side_effect = [RuntimeError("boom"), {"id": "ok"}]
    assert loop.force_close_all(NOW) == 1
    assert ex.close_position.call_count == 2


def test_force_close_continues_the_sweep_after_a_cancel_sweep_failure():
    """One symbol's list_orders failure must not abort the other symbols."""
    loop, alpaca, ex, _, book, _, _ = _build()
    book.add(_open_pos(symbol="AAA"))
    book.add(_open_pos(symbol="BBB"))
    alpaca.list_orders.side_effect = [RuntimeError("boom"), [{"id": "tp-b"}]]
    assert loop.force_close_all(NOW) == 2
    assert [c.args[0] for c in ex.close_position.call_args_list] == ["AAA", "BBB"]


# ── day reset ──────────────────────────────────────────────────────────

def test_reset_for_new_day_clears_the_just_exited_guard():
    """OrderExecutor.submit refuses a symbol in book._just_exited, and nothing
    else in this strategy clears it — so a symbol that stopped out once could
    never be entered again for the container's lifetime."""
    loop, _, _, _, book, _, _ = _build()
    book.add(_open_pos(symbol="AAA"))
    book.close("AAA", "opening_drive")
    assert book.was_just_exited("AAA") is True
    loop.reset_for_new_day()
    assert book.was_just_exited("AAA") is False


def test_reset_clears_day_state():
    loop, *_ = _build()
    loop.run_cut(NOW)
    loop.reset_for_new_day()
    assert loop.day.setups == {}
    assert loop.day.contexts == {}
    assert loop.day.watchlist == []
    assert loop.day.cut_done is False
    assert loop.day.eod_close_done is False


def test_reset_for_new_day_rolls_the_ledger_day():
    """FIX 2 — the consecutive-loss brake must not latch across sessions.

    DailyLedger.roll_day had no live caller in production (only
    backtest/intraday_replay.py), and consec_losses_system is cleared ONLY by
    a recorded win. Once two losing exits arm ConsecutiveLossFilter(limit=2,
    scope="system_wide"), every entry is rejected — so no win can ever be
    recorded and the strategy stays blocked for the container's lifetime.
    """
    from risk.filters import ConsecutiveLossFilter

    ledger = DailyLedger(initial_equity=100_000.0)
    loop, _, _, _, book, pm, _ = _build(ledger=ledger)
    loop.run_cut(NOW)

    # Two real losing exits through the managed phase arm the brake.
    for i, sym in enumerate(("AAA", "BBB")):
        book.add(_open_pos(symbol=sym))
        pm.on_bar.return_value = [_pa("stop", symbol=sym, price=98.0)]
        loop.manage_open(sym, _reclaim_bar(sym, minute=90 + i))
    assert ledger.consec_losses_system == 2

    brake = ConsecutiveLossFilter(limit=2, scope="system_wide")
    signal = MagicMock()
    signal.symbol = "CCC"
    assert brake.check(signal, None, ledger, book).passed is False

    next_day = DAY + timedelta(days=1)
    loop.reset_for_new_day(next_day)

    assert ledger.consec_losses_system == 0
    assert ledger.consecutive_losses_for("AAA") == 0
    assert ledger.day_pnl == 0.0
    assert ledger.trades_today == []
    assert ledger.day_started_at == next_day
    # And the brake is actually open again — this is the property that matters.
    assert brake.check(signal, None, ledger, book).passed is True


def test_reset_for_new_day_preserves_ledger_equity():
    """roll_day must not discard realised equity — only the per-day counters."""
    ledger = DailyLedger(initial_equity=100_000.0)
    loop, _, _, _, book, pm, _ = _build(ledger=ledger)
    loop.run_cut(NOW)
    book.add(_open_pos())
    pm.on_bar.return_value = [_pa("target", price=104.0)]
    loop.manage_open("AAA", _reclaim_bar("AAA", minute=90))
    assert ledger.equity == pytest.approx(100_040.0)
    loop.reset_for_new_day(DAY + timedelta(days=1))
    assert ledger.equity == pytest.approx(100_040.0)


# ── managed phase: rebuild must not reset PositionManager state (FIX 1) ──

class _RoundTripMySQL:
    """A store that returns exactly what it was last told to persist.

    ``load_open_positions`` builds BRAND-NEW OpenPosition objects out of the
    stored row values — which is precisely what
    ``MySQLStore.load_open_positions`` -> ``_dict_to_pos`` does. Nothing in
    this strategy ever calls ``sync_position_state``, so a stored row's
    ``bars_held`` / ``breakeven_moved`` / ``stop_px`` stay at whatever
    ``position_opened`` wrote at entry, forever.
    """

    def __init__(self, *positions: OpenPosition) -> None:
        self.rows = {(p.symbol, p.setup): replace(p) for p in positions}

    def load_open_positions(self) -> PositionBook:
        book = PositionBook()
        for row in self.rows.values():
            book.add(replace(row))
        return book


def _managed_bar(symbol: str, i: int, **kw) -> Bar:
    """One DISTINCT 5-minute managed-phase bar that triggers nothing.

    low 99.5 clears the 98.0 stop; high 101.0 is under both the 104.0 target
    and the 102.0 breakeven trigger (entry 100 + 1R of 2.0). Only
    max_hold_bars can end a position fed these bars.
    """
    return Bar(
        symbol=symbol, ts=DAY + timedelta(hours=1, minutes=5 * i),
        open=100.5, high=kw.get("high", 101.0), low=kw.get("low", 99.5),
        close=kw.get("close", 100.5), volume=50_000,
    )


def _real_pm_loop(mysql, *, max_hold_bars, breakeven_at_R=1.0):
    """A loop wired with a REAL PositionManager over a REAL PositionBook.

    No existing test combined the two: the run_day tests stub
    refresh_book_from_mysql and the loop tests use a MagicMock position
    manager, so nothing ever exercised a real rebuild against real
    PositionManager mutations — which is why this whole class of bug survived.
    """
    from core.position_manager import PositionManager

    book = PositionBook()
    pm = PositionManager(book, max_hold_bars=max_hold_bars,
                         breakeven_at_R=breakeven_at_R)
    loop, _, ex, _, _, _, _ = _build(book=book, position_manager=pm,
                                     mysql=mysql)
    loop.run_cut(NOW)
    loop.switch_to_regular_session_bars()
    return loop, book, ex


def _action_kinds(ex) -> list[str]:
    return [a.kind for c in ex.handle_actions.call_args_list for a in c.args[0]]


def test_max_hold_bars_fires_despite_the_per_iteration_book_rebuild():
    """FIX 1 — the discriminating case for the time stop.

    refresh_book_from_mysql runs once per managed-phase iteration (~60s) and
    replaces the book with brand-new objects carrying the row's bars_held.
    Nothing writes bars_held back to the row, so it is frozen at 0 and the
    counter oscillated 0 -> 1 -> 0 -> 1 for all ~270 iterations of a session:
    max_hold_bars: 36 could never be reached, and a position that never
    touched its stop or target rode to the 15:30 flatten instead of exiting
    at 14:00.
    """
    pos = _open_pos()
    pos.fill_confirmed = True                 # past PositionManager's fill gate
    mysql = _RoundTripMySQL(pos)
    loop, book, ex = _real_pm_loop(mysql, max_hold_bars=3)

    progression = []
    for i in range(4):
        loop.refresh_book_from_mysql()        # exactly what run_day does
        loop.manage_open("AAA", _managed_bar("AAA", i))
        live = book.get("AAA", "opening_drive")
        progression.append(live.bars_held if live is not None else None)

    assert "time_stop" in _action_kinds(ex), (
        "max_hold_bars never fired across the rebuilds; bars_held progression "
        f"was {progression} (expected [1, 2, 3, None])"
    )
    # None on the last step: PositionManager closed the book entry on exit.
    assert progression == [1, 2, 3, None]


def test_rebuild_preserves_a_breakeven_move_so_it_is_emitted_once():
    """FIX 1, same root cause — breakeven_moved/stop_px were also reset.

    Losing the flag made PositionManager re-emit `breakeven` on every later
    distinct bar, and OrderExecutor._move_equity_stop_to_breakeven then called
    replace_order against a stop_order_id the broker had already superseded.
    """
    pos = _open_pos()
    pos.fill_confirmed = True
    mysql = _RoundTripMySQL(pos)
    loop, book, ex = _real_pm_loop(mysql, max_hold_bars=50)

    # high 103.0 clears the 102.0 breakeven trigger; low 100.5 stays above the
    # post-move stop of 100.0 so the position survives every bar.
    for i in range(4):
        loop.refresh_book_from_mysql()
        loop.manage_open("AAA", _managed_bar("AAA", i, high=103.0, low=100.5,
                                             close=101.0))

    assert _action_kinds(ex).count("breakeven") == 1
    live = book.get("AAA", "opening_drive")
    assert live.breakeven_moved is True
    assert live.stop_px == pytest.approx(100.0)     # entry, not the row's 98.0


def test_rebuild_does_not_carry_bars_held_onto_a_new_position():
    """A NEW position on a symbol traded before must start at zero bars.

    At the 09:00 boot the in-memory book still holds yesterday's flattened
    rows, so carrying state forward on (symbol, setup) alone would time-stop
    today's fresh entry almost immediately. The entry order_id is the guard.
    """
    stale = _open_pos()
    stale.bars_held = 40
    book = PositionBook()
    book.add(stale)

    fresh = _open_pos()                      # same (symbol, setup)...
    fresh.order_id = "o-AAA-today"           # ...but a different entry order
    loop, *_ = _build(book=book, mysql=_RoundTripMySQL(fresh))

    assert loop.refresh_book_from_mysql() is True
    assert book.get("AAA", "opening_drive").bars_held == 0


def test_rebuild_failure_keeps_the_in_memory_managed_state():
    """A MySQL error must not silently zero bars_held either."""
    pos = _open_pos()
    pos.bars_held = 12
    book = PositionBook()
    book.add(pos)
    mysql = MagicMock()
    mysql.load_open_positions.side_effect = RuntimeError("boom")
    loop, *_ = _build(book=book, mysql=mysql)

    assert loop.refresh_book_from_mysql() is False
    assert book.get("AAA", "opening_drive").bars_held == 12


# ── Inclusive-end boundary: the cut-time bar is NOT part of the range ──

def test_run_cut_excludes_the_bar_at_the_cut():
    """Alpaca's `end` is inclusive, so a 09:30->10:00 request returns 31 bars.

    The 10:00 bar is the first bar of the ENTRY window. Including it is a real
    lookahead: or_high/or_close would incorporate a price the strategy has not
    seen at decision time, and bar_coverage would compute to 31/30 = 1.03.
    """
    loop, _, _, _, _, _, data = _build()
    _, or_end = loop.or_window(NOW)

    # Three in-range bars plus one stamped exactly at the cut, priced far above
    # the range so its inclusion would be unmistakable in or_high.
    def with_cut_bar(symbol, close, volume):
        bars = list(_or_bars(symbol, close, volume))
        bars.append(Bar(symbol=symbol, ts=or_end, open=close, high=close + 50.0,
                        low=close, close=close + 50.0, volume=volume))
        return bars

    data.get_bars_multi.return_value = {
        "AAA": with_cut_bar("AAA", 104.0, 60_000),
        "BBB": with_cut_bar("BBB", 104.0, 30_000),
        "SPY": with_cut_bar("SPY", 100.5, 30_000),
    }
    watchlist = loop.run_cut(NOW)
    assert watchlist, "expected candidates"
    for r in watchlist:
        assert r.metrics.or_high < 150.0, (
            f"{r.symbol}: or_high={r.metrics.or_high} includes the cut-time bar"
        )
        assert r.metrics.bar_coverage <= 1.0, (
            f"{r.symbol}: bar_coverage={r.metrics.bar_coverage} exceeds 1.0"
        )


def test_run_cut_seeded_context_also_excludes_the_cut_bar():
    """The seeded SessionContext must not carry the post-cut bar either, or
    session VWAP is computed from a price the range never saw."""
    loop, _, _, _, _, _, data = _build()
    _, or_end = loop.or_window(NOW)
    bars = list(_or_bars("AAA", 104.0, 60_000))
    bars.append(Bar(symbol="AAA", ts=or_end, open=104.0, high=154.0,
                    low=104.0, close=154.0, volume=60_000))
    data.get_bars_multi.return_value = {
        "AAA": bars,
        "SPY": list(_or_bars("SPY", 100.5, 30_000)),
    }
    loop.run_cut(NOW)
    ctx = loop.day.contexts["AAA"]
    assert ctx.bar_count == 3, f"expected 3 seeded bars, got {ctx.bar_count}"
    assert ctx.day_high < 150.0, f"context day_high={ctx.day_high} took the cut bar"
