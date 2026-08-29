# tests/test_opening_drive_loop.py
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from core.asset_class import AssetClassConfig
from core.bar import Bar
from risk.manager import RiskDecision
from scheduler.opening_drive_loop import OpeningDriveConfig, OpeningDriveLoop
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
    baselines = {s: _baseline() for s in list(universe) + ["SPY"]}
    scanner = OpeningDriveScanner(
        universe=universe, baselines=baselines,
        max_concurrent_positions=2, or_minutes=3,
    )
    cfg = OpeningDriveConfig(
        universe_path="config/universe_sp500_ndx100.csv",
        baselines_path="runtime/opening_drive/baselines.json",
        or_minutes=3, max_concurrent_positions=2,
    )
    alpaca, data = MagicMock(), MagicMock()
    rm, ex = MagicMock(), MagicMock()
    pm = MagicMock()
    book = PositionBook()
    rm.evaluate.return_value = RiskDecision(approved=True, qty=10, notional=1000)
    pm.on_bar.return_value = []

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
    )
    loop.fetch_prev_closes = MagicMock(return_value=prev_closes)
    return loop, alpaca, ex, rm, book, pm, data


# ── time helpers ───────────────────────────────────────────────────────

def test_or_window_is_0930_to_1000_ny():
    loop, *_ = _build()
    start, end = loop.or_window(DAY)
    assert start == datetime(2026, 8, 28, 13, 30, tzinfo=timezone.utc)
    assert end == datetime(2026, 8, 28, 14, 0, tzinfo=timezone.utc)


def test_cut_and_window_and_eod_times():
    loop, *_ = _build()
    assert loop.cut_time(DAY) == datetime(2026, 8, 28, 14, 0, tzinfo=timezone.utc)
    assert loop.entry_window_end(DAY) == datetime(2026, 8, 28, 15, 0, tzinfo=timezone.utc)
    assert loop.eod_close_time(DAY) == datetime(2026, 8, 28, 19, 30, tzinfo=timezone.utc)


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


# ── entry ──────────────────────────────────────────────────────────────

def _reclaim_bar(symbol: str, minute: int = 1) -> Bar:
    return Bar(symbol=symbol, ts=DAY + timedelta(minutes=minute),
               open=104.0, high=107.0, low=103.5, close=106.0, volume=90_000)


def test_on_bar_submits_when_trigger_and_risk_approve():
    loop, _, ex, rm, *_ = _build()
    loop.run_cut(NOW)
    loop.on_bar("AAA", _reclaim_bar("AAA"))
    assert rm.evaluate.called
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

def test_manage_open_routes_position_manager_actions():
    loop, _, ex, _, _, pm, _ = _build()
    loop.run_cut(NOW)
    pm.on_bar.return_value = ["action"]
    loop.manage_open("AAA", _reclaim_bar("AAA", minute=90))
    ex.handle_actions.assert_called_once()
    assert ex.handle_actions.call_args.kwargs["asset_class"] == "equity"


def test_manage_open_with_no_actions_does_not_call_executor():
    loop, _, ex, _, _, pm, _ = _build()
    loop.run_cut(NOW)
    pm.on_bar.return_value = []
    loop.manage_open("AAA", _reclaim_bar("AAA", minute=90))
    assert not ex.handle_actions.called


# ── EOD flatten ────────────────────────────────────────────────────────

def _open_pos(symbol="AAA", setup="opening_drive", **kw) -> OpenPosition:
    return OpenPosition(
        symbol=symbol, setup=setup, side="long", qty=10, entry_px=100.0,
        stop_px=98.0, target_px=104.0, opened_at=NOW, order_id=f"o-{symbol}",
        stop_order_id=kw.get("stop_order_id", "stop-1"),
        target_order_id=kw.get("target_order_id", "tgt-1"),
    )


def test_force_close_cancels_oco_children_before_closing():
    """Flattening with live stop/target legs leaves orphaned orders."""
    loop, alpaca, ex, _, book, _, _ = _build()
    book.add(_open_pos())
    calls: list[str] = []
    alpaca.cancel_order.side_effect = lambda oid: calls.append(f"cancel:{oid}")
    ex.close_position.side_effect = lambda *a, **k: calls.append("close")
    assert loop.force_close_all(NOW) == 1
    assert calls.index("cancel:stop-1") < calls.index("close")
    assert calls.index("cancel:tgt-1") < calls.index("close")


def test_force_close_still_closes_when_cancel_fails():
    """An already-filled leg raises on cancel; the close must still happen."""
    loop, alpaca, ex, _, book, _, _ = _build()
    book.add(_open_pos())
    alpaca.cancel_order.side_effect = RuntimeError("order not cancelable")
    assert loop.force_close_all(NOW) == 1
    assert ex.close_position.called


def test_force_close_ignores_other_strategies_positions():
    loop, _, ex, _, book, _, _ = _build()
    book.add(_open_pos(symbol="TQQQ", setup="sma_slope"))
    assert loop.force_close_all(NOW) == 0
    assert not ex.close_position.called


def test_force_close_handles_positions_without_oco_ids():
    loop, alpaca, ex, _, book, _, _ = _build()
    book.add(_open_pos(stop_order_id=None, target_order_id=None))
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


# ── day reset ──────────────────────────────────────────────────────────

def test_reset_clears_day_state():
    loop, *_ = _build()
    loop.run_cut(NOW)
    loop.reset_for_new_day()
    assert loop.day.setups == {}
    assert loop.day.contexts == {}
    assert loop.day.watchlist == []
    assert loop.day.cut_done is False
    assert loop.day.eod_close_done is False
