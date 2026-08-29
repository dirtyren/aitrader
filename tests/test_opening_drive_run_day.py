"""run_day scheduling behaviour for the Opening Drive trader.

These tests drive main_opening_drive.run_day on a FAKE CLOCK: `_now` is
monkeypatched to a Clock, and the same Clock's `sleep` is passed as the
sleeper, so every `sleeper(60)` advances wall time by 60 seconds. The phase
loops therefore execute for real, once per simulated iteration, and we can
assert on what happens per iteration and in what order — which is what the
per-cycle defects (stale cash snapshot, stale book, latched DTBP flag,
re-running the whole day every 10 minutes) actually are.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import main_opening_drive as mod
from strategies.opening_drive_scanner import OpeningDriveBaseline

# 2026-08-28 is a Friday. All wall-clock values below are UTC; NY is UTC-4.
BOOT = datetime(2026, 8, 28, 13, 0, tzinfo=timezone.utc)      # 09:00 NY
CUT = datetime(2026, 8, 28, 14, 0, tzinfo=timezone.utc)       # 10:00 NY
ENTRY_END = datetime(2026, 8, 28, 15, 0, tzinfo=timezone.utc)  # 11:00 NY
EOD = datetime(2026, 8, 28, 19, 30, tzinfo=timezone.utc)      # 15:30 NY
BASELINE_T = datetime(2026, 8, 28, 20, 10, tzinfo=timezone.utc)  # 16:10 NY
NEXT_BOOT = datetime(2026, 8, 29, 13, 0, tzinfo=timezone.utc)  # 09:00 NY next


class Clock:
    def __init__(self, start: datetime) -> None:
        self.t = start

    def now(self) -> datetime:
        return self.t

    def sleep(self, secs: float) -> None:
        self.t += timedelta(seconds=float(secs))


def _fresh_baseline(now: datetime) -> OpeningDriveBaseline:
    return OpeningDriveBaseline(
        atr_14d=4.0, avg_or_volume_20d=50_000.0,
        avg_daily_volume_20d=400_000.0,
        computed_at=now - timedelta(days=1),
    )


def _stub_loop(*, baselines: dict | None = None, positions=("AAA",)):
    """A MagicMock loop with REAL time helpers and a real _DayState-shaped day."""
    loop = MagicMock()
    loop.cut_time.side_effect = lambda d: mod._ny_dt(d, 10, 0)
    loop.entry_window_end.side_effect = lambda d: mod._ny_dt(d, 11, 0)
    loop.eod_close_time.side_effect = lambda d: mod._ny_dt(d, 15, 30)
    loop.day = SimpleNamespace(
        watchlist=[], setups={}, contexts={}, cut_done=False,
        eod_close_done=False, post_close_refresh_done=False,
        last_managed_bar_ts={},
    )
    loop.cfg = SimpleNamespace(
        premarket_bar_timeframe="1Min", regular_bar_timeframe="5Min",
        baselines_max_age_days=7, or_minutes=30, lookback_sessions=20,
        baselines_path="runtime/opening_drive/baselines.json",
    )
    loop.scanner.baselines = (
        {"AAA": _fresh_baseline(BOOT)} if baselines is None else baselines
    )
    loop.book.all.return_value = [
        SimpleNamespace(symbol=s, setup="opening_drive", side="long", qty=10)
        for s in positions
    ]
    loop.data.get_bars.return_value = []
    return loop


@pytest.fixture
def harness(monkeypatch):
    """(clock, loop, alpaca, rm, events) with the network stubbed out."""
    clock = Clock(BOOT)
    monkeypatch.setattr(mod, "_now", clock.now)
    monkeypatch.setattr(mod, "_shutdown", False)

    events: list[tuple[str, datetime]] = []

    loop = _stub_loop()
    loop.run_cut.side_effect = lambda now: events.append(("cut", clock.now()))
    loop.executor.reset_cycle.side_effect = lambda: events.append(
        ("reset_cycle", clock.now()))
    loop.refresh_book_from_mysql.side_effect = lambda: events.append(
        ("book_refresh", clock.now()))
    loop.force_close_all.side_effect = lambda now: events.append(
        ("flatten", clock.now()))

    alpaca, rm = MagicMock(), MagicMock()
    monkeypatch.setattr(
        mod, "refresh_equity_and_cash",
        lambda a, r: events.append(("cash", clock.now())),
    )
    monkeypatch.setattr(
        mod, "refresh_baselines_post_close",
        lambda lp, now: (events.append(("baseline_build", clock.now())), 5)[1],
    )
    monkeypatch.setattr(mod, "_fetch_entry_bars", lambda lp, now, since: {})
    return SimpleNamespace(clock=clock, loop=loop, alpaca=alpaca, rm=rm,
                           events=events)


def _run(h, *, anchor=None):
    mod.run_day(h.loop, h.rm, h.alpaca, day_anchor=anchor or h.clock.now(),
                sleeper=h.clock.sleep)


def _at(events, kind, lo, hi):
    """Events of `kind` whose timestamp falls in [lo, hi)."""
    return [ts for k, ts in events if k == kind and lo <= ts < hi]


# ── C3: the DTBP latch must not survive the day ────────────────────────

def test_run_day_resets_the_dtbp_latch_before_the_entry_window(harness):
    """C3 — the discriminating case.

    OrderExecutor latches _dtbp_exhausted on ONE day-trading-buying-power
    rejection and only reset_cycle() clears it. main_opening_drive never
    called it, so a single rejection silently stopped every entry for the
    container's lifetime while the logs looked normal.
    """
    _run(harness)
    resets = [ts for k, ts in harness.events if k == "reset_cycle"]
    cut = next(ts for k, ts in harness.events if k == "cut")
    assert resets, "executor.reset_cycle() was never called"
    assert min(resets) <= cut, "the latch was not cleared before the cut"


def test_run_day_resets_the_dtbp_latch_every_entry_window_iteration(harness):
    """One rejection must not suppress entries for the rest of the window."""
    _run(harness)
    in_window = _at(harness.events, "reset_cycle", CUT, ENTRY_END)
    assert len(in_window) >= 55, (
        f"reset_cycle ran {len(in_window)} times inside the 60-minute entry "
        "window; a DTBP rejection at 10:05 would block every later entry"
    )


# ── I6: available_cash must be refreshed inside the entry window ───────

def test_run_day_refreshes_cash_every_entry_window_iteration(harness):
    """I6 — the discriminating case.

    All five entries between 10:00 and 11:00 used to be sized against the
    single 10:00 snapshot. If sma_slope opens its TQQQ position inside that
    hour, the snapshot is stale in the unsafe direction, Alpaca rejects, and
    C3's latch fires.
    """
    _run(harness)
    in_window = _at(harness.events, "cash", CUT, ENTRY_END)
    assert len(in_window) >= 55, (
        f"available_cash was refreshed {len(in_window)} times during the "
        "entry window — sizing would use one stale 10:00 snapshot"
    )


# ── C2: the book must be rebuilt from MySQL ───────────────────────────

def test_run_day_refreshes_the_book_before_the_cut(harness):
    """Yesterday's flattened positions must be gone before today's entries,
    or ConcurrentPositionFilter rejects every signal."""
    _run(harness)
    cut = next(ts for k, ts in harness.events if k == "cut")
    refreshes = [ts for k, ts in harness.events if k == "book_refresh"]
    assert refreshes and min(refreshes) <= cut


def test_run_day_refreshes_the_book_every_managed_iteration(harness):
    """C2 — the discriminating case: the book was loaded once at boot."""
    _run(harness)
    in_managed = _at(harness.events, "book_refresh", ENTRY_END, EOD)
    assert len(in_managed) >= 250, (
        f"the book was rebuilt {len(in_managed)} times during the managed "
        "phase; a reconciler-applied close would never be seen"
    )


# ── C4: boot-time staleness fallback ──────────────────────────────────

def test_run_day_refreshes_stale_baselines_before_the_cut(harness):
    """C4 — the discriminating case.

    The only build was at 16:10, AFTER the session. baselines_are_stale had
    no production caller at all, so baselines_max_age_days: 7 was inert and
    7-14 day old baselines were used silently.
    """
    harness.loop.scanner.baselines = {
        "AAA": OpeningDriveBaseline(
            atr_14d=4.0, avg_or_volume_20d=1.0, avg_daily_volume_20d=1.0,
            computed_at=BOOT - timedelta(days=9),
        ),
    }
    _run(harness)
    builds = [ts for k, ts in harness.events if k == "baseline_build"]
    cut = next(ts for k, ts in harness.events if k == "cut")
    assert builds, "stale baselines were never refreshed"
    assert min(builds) <= cut, "the refresh ran after the cut it must feed"


def test_fresh_baselines_are_not_rebuilt_before_the_cut(harness):
    """The pre-cut refresh is a FALLBACK — it must not re-run the 20-session
    bulk fetch every morning when 16:10 already did the work."""
    _run(harness)
    pre_cut = _at(harness.events, "baseline_build", BOOT, CUT)
    assert pre_cut == []


def test_boot_at_0915_with_no_baselines_file_can_still_trade_today(
    monkeypatch, harness,
):
    """A deploy at 09:15 with no baselines file must end up able to trade."""
    harness.clock.t = datetime(2026, 8, 28, 13, 15, tzinfo=timezone.utc)
    harness.loop.scanner.baselines = {}          # load_baselines({}) on no file

    def _build(lp, now):
        harness.events.append(("baseline_build", harness.clock.now()))
        lp.scanner.baselines = {"AAA": _fresh_baseline(now)}
        return 1

    monkeypatch.setattr(mod, "refresh_baselines_post_close", _build)
    _run(harness)
    builds = [ts for k, ts in harness.events if k == "baseline_build"]
    assert builds[0] < CUT
    assert harness.loop.scanner.baselines, "no baselines available for the cut"
    assert any(k == "cut" for k in (e[0] for e in harness.events))


# ── I3: the day must not re-run every ten minutes until midnight ───────

def test_post_close_baseline_rebuild_runs_once_per_day(harness):
    """I3 — the discriminating case.

    After 16:10, _sleep_until returned immediately and main() restarted
    run_day every 600s, so 16:10-00:00 produced ~47 full rebuilds (20
    sessions x ~519 symbols of bulk 1-minute bars each) and reset computed_at
    every time, making staleness meaningless.
    """
    _run(harness)
    harness.clock.t = BASELINE_T + timedelta(minutes=10)
    _run(harness)                      # same day, no reset_for_new_day
    builds = [ts for k, ts in harness.events if k == "baseline_build"]
    assert len(builds) == 1, (
        f"the 20-session baseline rebuild ran {len(builds)} times in one day"
    )


def test_run_day_tail_sleeps_until_the_next_session(harness):
    """run_day must not return minutes after the close — that is what let
    main()'s 600s loop re-run the whole day ~47 times."""
    _run(harness)
    assert harness.clock.now() >= NEXT_BOOT, (
        f"run_day returned at {harness.clock.now()}, before the next "
        f"session's boot window at {NEXT_BOOT}"
    )


# ── I5/I8: bar timeframes come from config ────────────────────────────

def test_managed_phase_uses_the_configured_regular_timeframe(
    monkeypatch, harness,
):
    """OpeningDriveConfig.regular_bar_timeframe was populated from YAML and
    then ignored — the loop hardcoded "5Min"."""
    harness.loop.cfg.regular_bar_timeframe = "15Min"
    _run(harness)
    timeframes = {c.args[2] for c in harness.loop.data.get_bars.call_args_list}
    assert timeframes == {"15Min"}


def test_entry_window_uses_the_configured_premarket_timeframe(monkeypatch):
    """_fetch_entry_bars hardcoded "1Min" instead of reading the config."""
    calls: list[str] = []

    class FakeData:
        def get_bars(self, sym, ac, timeframe, start, end, use_cache=True):
            calls.append(timeframe)
            return []

    loop = _stub_loop()
    loop.cfg.premarket_bar_timeframe = "2Min"
    loop.data = FakeData()
    r = MagicMock()
    r.symbol = "AAA"
    loop.day.watchlist = [r]
    mod._fetch_entry_bars(loop, CUT + timedelta(minutes=1), CUT)
    assert calls == ["2Min"]
