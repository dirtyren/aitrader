"""Integration tests for the Gap-and-Go daily lifecycle orchestrator.

Drives every phase of GapAndGoLoop in sequence with a mocked Alpaca client
and a fixture-injected snapshot provider. Avoids network and time.sleep.
"""
from __future__ import annotations
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from broker.order_executor import OrderExecutor
from core.asset_class import AssetClassConfig
from core.bar import Bar
from risk.manager import RiskDecision, RiskManager
from scheduler.gap_and_go_loop import GapAndGoConfig, GapAndGoLoop
from state.daily_ledger import DailyLedger
from state.position_book import OpenPosition, PositionBook
from strategies.gap_scanner import GapScanner, ScannerFilters, ScannerRanking, _Baseline
from strategies.setup_gap_and_go import GapAndGoSetup


_EQ = AssetClassConfig(
    name="equity", timezone="America/New_York",
    session_open_local="04:00", session_close_local="20:00",
    opening_blackout_min=0, bar_timeframe="1Min",
    slippage_bps=2.0, commission_per_share=0.0, commission_bps=0.0,
)

# 08:30 ET on 2026-05-29 in UTC (EDT = UTC-4 → 12:30 UTC).
_DAY = datetime(2026, 5, 29, 12, 30, tzinfo=timezone.utc)
_REG_OPEN_UTC = datetime(2026, 5, 29, 13, 30, tzinfo=timezone.utc)
_EOD_UTC = datetime(2026, 5, 29, 19, 55, tzinfo=timezone.utc)


def _baseline(atr=2.0, premkt_vol=200_000, daily_vol=50_000_000,
              age_days: float = 0.0) -> _Baseline:
    return _Baseline(
        atr_14d=atr,
        avg_premarket_volume_20d=premkt_vol,
        avg_daily_volume_20d=daily_vol,
        computed_at=_DAY - timedelta(days=age_days),
    )


def _build_loop(*, snapshots_provider=None) -> tuple[GapAndGoLoop, MagicMock,
                                                     PositionBook, MagicMock]:
    """Wire a loop with mocked broker + risk for integration testing."""
    universe = ["AAPL", "MSFT"]
    scanner = GapScanner(
        universe=universe,
        baselines={s: _baseline() for s in universe},
        baselines_max_age_days=7,
        filters=ScannerFilters(),
        ranking=ScannerRanking(candidate_multiplier=1.5),
        max_concurrent_positions=4,
    )
    cfg = GapAndGoConfig(
        universe_path="ignored",
        baselines_path="ignored",
        baselines_max_age_days=7,
    )

    book = PositionBook()
    alpaca = MagicMock()
    alpaca.submit_order.return_value = {"id": "eh-1"}
    alpaca.attach_oco.return_value = {"id": "oco-1"}
    alpaca.get_positions.return_value = []  # default: no broker fills

    ledger = DailyLedger(initial_equity=100_000.0)

    risk_manager = MagicMock(spec=RiskManager)
    risk_manager.evaluate.return_value = RiskDecision(
        approved=True, qty=10, notional=2000.0,
    )

    executor = OrderExecutor(alpaca, book, strategy_name="gap_and_go",
                             logger=MagicMock())
    position_manager = MagicMock()
    position_manager.on_bar.return_value = []

    loop = GapAndGoLoop(
        cfg=cfg,
        scanner=scanner,
        equity_asset_class=_EQ,
        alpaca_client=alpaca,
        alpaca_data=MagicMock(),
        risk_manager=risk_manager,
        executor=executor,
        book=book,
        position_manager=position_manager,
        ledger=ledger,
        strategy_name="gap_and_go",
        snapshots_provider=snapshots_provider,
    )
    return loop, alpaca, book, position_manager


def _snapshot(last=210.0, prev_close=200.0,
              h=212.0, l=199.0, v=2_000_000.0, vw=210.5) -> dict:
    return {
        "latestTrade": {"p": last},
        "minuteBar": {"h": h, "l": l, "v": v, "vw": vw, "c": last},
        "prevDailyBar": {"c": prev_close},
    }


def _bar(ts, o, h, l, c, v=1000.0, symbol="AAPL") -> Bar:
    return Bar(symbol=symbol, ts=ts, open=o, high=h, low=l, close=c, volume=v)


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------


def test_time_helpers_resolve_to_ny_local():
    loop, *_ = _build_loop()
    assert loop.cut_time(_DAY) == _DAY                        # 08:30 ET
    assert loop.regular_open_time(_DAY) == _REG_OPEN_UTC      # 09:30 ET
    assert loop.deadline_time(_DAY) == _REG_OPEN_UTC          # default 60-min window
    assert loop.eod_close_time(_DAY) == _EOD_UTC              # 15:55 ET


# ---------------------------------------------------------------------------
# Cut + setup build
# ---------------------------------------------------------------------------


def test_run_cut_builds_setups_and_contexts():
    snaps = {
        "AAPL": _snapshot(last=210.0, prev_close=200.0, v=2_000_000.0),
        "MSFT": _snapshot(last=190.0, prev_close=200.0),  # negative gap → rejected
    }
    loop, *_ = _build_loop(snapshots_provider=lambda u: snaps)

    loop.poll_snapshot(_DAY - timedelta(hours=1))  # 07:30 ET
    watchlist = loop.run_cut(_DAY)
    assert [r.symbol for r in watchlist] == ["AAPL"]
    assert "AAPL" in loop.day.setups
    assert "AAPL" in loop.day.contexts
    assert loop.day.setups["AAPL"].entry_deadline == _REG_OPEN_UTC


def test_run_cut_is_idempotent_within_day():
    snaps = {"AAPL": _snapshot(v=2_000_000.0)}
    loop, *_ = _build_loop(snapshots_provider=lambda u: snaps)
    loop.poll_snapshot(_DAY - timedelta(hours=1))

    first = loop.run_cut(_DAY)
    second = loop.run_cut(_DAY)
    assert first is second or first == second


# ---------------------------------------------------------------------------
# Bar entry path
# ---------------------------------------------------------------------------


def test_on_bar_emits_extended_hours_entry_on_breakout():
    snaps = {"AAPL": _snapshot(last=210.0, prev_close=200.0, v=2_000_000.0,
                               h=212.0, l=199.0)}
    loop, alpaca, book, _ = _build_loop(snapshots_provider=lambda u: snaps)
    loop.poll_snapshot(_DAY - timedelta(hours=1))
    loop.run_cut(_DAY)

    setup = loop.day.setups["AAPL"]
    assert setup.premarket_high == 212.0  # snapshot fed running high

    # Seed 5 quiet 1-min bars below PMH for the trailing-volume window.
    seed_ts = _DAY
    for i in range(5):
        loop.on_bar("AAPL",
                    _bar(seed_ts + timedelta(minutes=i),
                         o=211.0, h=211.5, l=210.5, c=211.0, v=500.0))
    assert book.count() == 0  # no trigger yet

    # Trigger bar: close 0.1 above PMH=212 on 4x volume (well under 0.5%
    # slippage cap: 0.1/212 = 0.047%).
    loop.on_bar("AAPL",
                _bar(seed_ts + timedelta(minutes=5),
                     o=211.5, h=212.05, l=211.4, c=212.05, v=2000.0))

    assert book.count() == 1
    pos = next(iter(book.all()))
    assert pos.symbol == "AAPL"
    assert pos.pending_oco_attach is True
    # The executor must have used the extended-hours plain-limit path.
    alpaca.submit_order.assert_called_once()
    kwargs = alpaca.submit_order.call_args.kwargs
    assert kwargs["extended_hours"] is True
    assert kwargs["order_type"] == "limit"


def test_on_bar_skips_when_setup_or_context_missing():
    """Symbols not on the watchlist are silently ignored."""
    loop, alpaca, book, _ = _build_loop()
    # No cut run — day.setups is empty.
    loop.on_bar("AAPL", _bar(_DAY, 200, 201, 199, 200, v=1000))
    assert book.count() == 0
    alpaca.submit_order.assert_not_called()


def test_on_bar_respects_risk_rejection():
    snaps = {"AAPL": _snapshot(v=2_000_000.0, h=212.0, l=199.0)}
    loop, alpaca, book, _ = _build_loop(snapshots_provider=lambda u: snaps)
    loop.poll_snapshot(_DAY - timedelta(hours=1))
    loop.run_cut(_DAY)

    loop.risk_manager.evaluate.return_value = RiskDecision.reject("circuit_breaker")

    seed_ts = _DAY
    for i in range(5):
        loop.on_bar("AAPL",
                    _bar(seed_ts + timedelta(minutes=i),
                         o=211.0, h=211.5, l=210.5, c=211.0, v=500.0))
    loop.on_bar("AAPL",
                _bar(seed_ts + timedelta(minutes=5),
                     o=211.5, h=212.05, l=211.4, c=212.05, v=2000.0))

    assert book.count() == 0
    alpaca.submit_order.assert_not_called()


# ---------------------------------------------------------------------------
# 09:30 OCO attach + timeframe switch
# ---------------------------------------------------------------------------


def test_attach_premarket_brackets_calls_post_open_attach():
    loop, alpaca, book, _ = _build_loop()
    book.add(OpenPosition(
        symbol="AAPL", setup="gap_and_go", side="long", qty=10,
        entry_px=212.0, stop_px=199.0, target_px=215.0,
        opened_at=_DAY, order_id="entry-1",
        pending_oco_attach=True,
    ))
    alpaca.get_positions.return_value = [{"symbol": "AAPL", "qty": "10"}]

    summary = loop.attach_premarket_brackets(_REG_OPEN_UTC)
    assert summary == {"attached": 1, "failsafe_closed": 0, "skipped": 0}
    alpaca.attach_oco.assert_called_once()
    pos = next(iter(book.all()))
    assert pos.pending_oco_attach is False


def test_attach_premarket_brackets_idempotent_within_day():
    loop, alpaca, book, _ = _build_loop()
    book.add(OpenPosition(
        symbol="AAPL", setup="gap_and_go", side="long", qty=10,
        entry_px=212.0, stop_px=199.0, target_px=215.0,
        opened_at=_DAY, order_id="entry-1",
        pending_oco_attach=True,
    ))
    alpaca.get_positions.return_value = [{"symbol": "AAPL", "qty": "10"}]

    loop.attach_premarket_brackets(_REG_OPEN_UTC)
    summary = loop.attach_premarket_brackets(_REG_OPEN_UTC)
    assert summary == {"attached": 0, "failsafe_closed": 0, "skipped": 0}
    assert alpaca.attach_oco.call_count == 1


def test_switch_to_regular_session_bars_resets_contexts():
    snaps = {"AAPL": _snapshot(v=2_000_000.0, h=212.0, l=199.0)}
    loop, *_ = _build_loop(snapshots_provider=lambda u: snaps)
    loop.poll_snapshot(_DAY - timedelta(hours=1))
    loop.run_cut(_DAY)

    # Ingest a couple of pre-market bars; the context should hold them.
    for i in range(2):
        loop.day.contexts["AAPL"].ingest(
            _bar(_DAY + timedelta(minutes=i), o=211, h=211.5, l=210.5, c=211, v=500),
        )
    assert loop.day.contexts["AAPL"].bar_count == 2

    loop.switch_to_regular_session_bars()
    assert loop.day.contexts["AAPL"].bar_count == 0


# ---------------------------------------------------------------------------
# Managed phase + EOD
# ---------------------------------------------------------------------------


def test_manage_open_routes_actions_through_executor():
    loop, alpaca, book, pm = _build_loop()
    loop.day.contexts["AAPL"] = type(loop.day.contexts).__new__(type(loop.day.contexts))  # placeholder
    # Use a real SessionContext for ingest:
    from core.session import SessionContext
    loop.day.contexts["AAPL"] = SessionContext(symbol="AAPL", asset_class=_EQ)

    action = MagicMock(kind="time_stop", symbol="AAPL", side="long",
                      qty=10, price=211.0, setup="gap_and_go")
    pm.on_bar.return_value = [action]

    loop.executor.handle_actions = MagicMock()
    loop.manage_open("AAPL", _bar(_REG_OPEN_UTC, 212, 213, 211, 212, v=1000))

    pm.on_bar.assert_called_once()
    loop.executor.handle_actions.assert_called_once()
    assert loop.executor.handle_actions.call_args.kwargs["asset_class"] == "equity"


def test_force_close_all_closes_only_gap_and_go_positions():
    loop, alpaca, book, _ = _build_loop()
    book.add(OpenPosition(
        symbol="AAPL", setup="gap_and_go", side="long", qty=10,
        entry_px=212.0, stop_px=199.0, target_px=215.0,
        opened_at=_DAY, order_id="entry-1",
    ))
    book.add(OpenPosition(
        symbol="MSFT", setup="vwap_bounce", side="long", qty=5,
        entry_px=400.0, stop_px=395.0, target_px=410.0,
        opened_at=_DAY, order_id="entry-2",
    ))
    loop.executor.close_position = MagicMock()

    closed = loop.force_close_all(_EOD_UTC)
    assert closed == 1
    loop.executor.close_position.assert_called_once_with(
        "AAPL", "long", 10, asset_class="equity",
    )


def test_force_close_all_idempotent_within_day():
    loop, alpaca, book, _ = _build_loop()
    book.add(OpenPosition(
        symbol="AAPL", setup="gap_and_go", side="long", qty=10,
        entry_px=212.0, stop_px=199.0, target_px=215.0,
        opened_at=_DAY, order_id="entry-1",
    ))
    loop.executor.close_position = MagicMock()
    loop.force_close_all(_EOD_UTC)
    closed = loop.force_close_all(_EOD_UTC)
    assert closed == 0
    assert loop.executor.close_position.call_count == 1


def test_reset_for_new_day_clears_state():
    snaps = {"AAPL": _snapshot(v=2_000_000.0, h=212.0, l=199.0)}
    loop, *_ = _build_loop(snapshots_provider=lambda u: snaps)
    loop.poll_snapshot(_DAY - timedelta(hours=1))
    loop.run_cut(_DAY)
    assert loop.day.cut_done is True
    assert "AAPL" in loop.day.setups

    loop.reset_for_new_day()
    assert loop.day.cut_done is False
    assert loop.day.setups == {}
    assert loop.scanner.get_state("AAPL").premarket_volume == 0


# ---------------------------------------------------------------------------
# Full-day end-to-end smoke
# ---------------------------------------------------------------------------


def test_full_day_smoke_pre_open_to_eod():
    """Walk every phase: poll → cut → entry → 09:30 attach → manage → EOD close."""
    snaps = {"AAPL": _snapshot(last=212.0, prev_close=200.0, v=2_000_000.0,
                               h=212.0, l=199.0)}
    loop, alpaca, book, pm = _build_loop(snapshots_provider=lambda u: snaps)

    # Phase 2: poll
    loop.poll_snapshot(_DAY - timedelta(hours=4))  # 04:30 ET
    loop.poll_snapshot(_DAY - timedelta(minutes=5))  # 08:25 ET
    # Phase 3: cut
    watchlist = loop.run_cut(_DAY)
    assert [r.symbol for r in watchlist] == ["AAPL"]

    # Phase 4: bars + entry. Seed quiet bars then trigger.
    for i in range(5):
        loop.on_bar("AAPL",
                    _bar(_DAY + timedelta(minutes=i),
                         o=211.5, h=211.9, l=211.0, c=211.5, v=500.0))
    loop.on_bar("AAPL",
                _bar(_DAY + timedelta(minutes=5),
                     o=211.6, h=212.05, l=211.5, c=212.05, v=2000.0))
    assert book.count() == 1
    pos = next(iter(book.all()))
    assert pos.pending_oco_attach is True

    # Phase 5: 09:30 OCO attach
    alpaca.get_positions.return_value = [{"symbol": "AAPL",
                                          "qty": str(pos.qty)}]
    summary = loop.attach_premarket_brackets(_REG_OPEN_UTC)
    assert summary["attached"] == 1
    assert pos.pending_oco_attach is False
    loop.switch_to_regular_session_bars()
    assert loop.day.contexts["AAPL"].bar_count == 0

    # Phase 6: managed bar (no PM action this tick)
    loop.manage_open("AAPL",
                     _bar(_REG_OPEN_UTC + timedelta(minutes=5),
                          o=212.5, h=213.0, l=212.0, c=212.7, v=1000))
    pm.on_bar.assert_called()

    # Phase 7: EOD close
    loop.executor.close_position = MagicMock()
    closed = loop.force_close_all(_EOD_UTC)
    assert closed == 1
