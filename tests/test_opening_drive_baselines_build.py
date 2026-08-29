# tests/test_opening_drive_baselines_build.py
from datetime import date, datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from core.bar import Bar
from scripts.build_opening_drive_baselines import build_baselines, session_dates

NOW = datetime(2026, 8, 28, 20, 10, tzinfo=timezone.utc)   # 16:10 NY


def _daily(symbol: str, day: date, close: float, volume: float) -> Bar:
    return Bar(symbol=symbol, ts=datetime(day.year, day.month, day.day,
                                          20, 0, tzinfo=timezone.utc),
               open=close - 1, high=close + 1, low=close - 2,
               close=close, volume=volume)


def _minute(symbol: str, day: date, minute: int, volume: float) -> Bar:
    ts = datetime(day.year, day.month, day.day, 13, 30, tzinfo=timezone.utc)
    return Bar(symbol=symbol, ts=ts + timedelta(minutes=minute),
               open=100.0, high=101.0, low=99.0, close=100.5, volume=volume)


DAYS = [date(2026, 8, 24), date(2026, 8, 25), date(2026, 8, 26),
        date(2026, 8, 27), date(2026, 8, 28)]


def test_session_dates_excludes_today_and_takes_last_n():
    spy = [_daily("SPY", d, 500.0, 1_000) for d in DAYS]
    assert session_dates(spy, NOW, 3) == DAYS[1:4]   # 25, 26, 27 — not 28


def test_session_dates_handles_fewer_sessions_than_requested():
    spy = [_daily("SPY", DAYS[0], 500.0, 1_000)]
    assert session_dates(spy, NOW, 20) == [DAYS[0]]


def test_session_dates_empty_when_only_today_present():
    spy = [_daily("SPY", DAYS[-1], 500.0, 1_000)]
    assert session_dates(spy, NOW, 5) == []


def _data_mock(daily_volume: float = 400_000.0,
               or_volume_per_bar: float = 1_000.0,
               symbols=("AAA", "SPY")) -> MagicMock:
    data = MagicMock()

    def _get(syms, asset_class, timeframe, start, end):
        if timeframe == "1Day":
            return {
                s: [_daily(s, d, 100.0 + i, daily_volume)
                    for i, d in enumerate(DAYS)]
                for s in symbols
            }
        day = start.date()
        return {
            s: [_minute(s, day, m, or_volume_per_bar) for m in range(3)]
            for s in symbols
        }

    data.get_bars_multi.side_effect = _get
    return data


def test_builds_baseline_per_symbol():
    data = _data_mock()
    out = build_baselines(data, ["AAA", "SPY"], NOW,
                          or_minutes=3, lookback_sessions=3, atr_window=2)
    assert set(out) == {"AAA", "SPY"}
    assert out["AAA"].atr_14d == pytest.approx(3.0)
    assert out["AAA"].avg_daily_volume_20d == pytest.approx(400_000.0)
    # 3 bars x 1000 per session, averaged over 3 sessions
    assert out["AAA"].avg_or_volume_20d == pytest.approx(3_000.0)
    assert out["AAA"].computed_at == NOW


def test_includes_the_benchmark():
    """SPY needs a baseline too — run_cut checks its bar_coverage."""
    data = _data_mock()
    out = build_baselines(data, ["AAA", "SPY"], NOW,
                          or_minutes=3, lookback_sessions=3, atr_window=2)
    assert "SPY" in out


def test_skips_symbol_with_no_daily_bars():
    data = MagicMock()

    def _get(syms, asset_class, timeframe, start, end):
        if timeframe == "1Day":
            return {"SPY": [_daily("SPY", d, 500.0, 1_000) for d in DAYS]}
        return {"SPY": [_minute("SPY", start.date(), m, 1_000)
                        for m in range(3)]}

    data.get_bars_multi.side_effect = _get
    out = build_baselines(data, ["AAA", "SPY"], NOW,
                          or_minutes=3, lookback_sessions=3, atr_window=2)
    assert "AAA" not in out


def test_skips_symbol_with_no_or_volume():
    data = _data_mock(or_volume_per_bar=0.0)
    out = build_baselines(data, ["AAA", "SPY"], NOW,
                          or_minutes=3, lookback_sessions=3, atr_window=2)
    assert out == {}


def test_skips_symbol_with_insufficient_bars_for_atr():
    data = _data_mock()
    out = build_baselines(data, ["AAA", "SPY"], NOW,
                          or_minutes=3, lookback_sessions=3, atr_window=50)
    assert out == {}


def test_returns_empty_when_no_prior_sessions_available():
    data = MagicMock()
    data.get_bars_multi.return_value = {
        "SPY": [_daily("SPY", DAYS[-1], 500.0, 1_000)],
        "AAA": [_daily("AAA", DAYS[-1], 100.0, 400_000)],
    }
    out = build_baselines(data, ["AAA", "SPY"], NOW,
                          or_minutes=3, lookback_sessions=3, atr_window=2)
    assert out == {}


def test_issues_one_daily_request_plus_one_per_session():
    data = _data_mock()
    build_baselines(data, ["AAA", "SPY"], NOW,
                    or_minutes=3, lookback_sessions=3, atr_window=2)
    timeframes = [c.args[2] for c in data.get_bars_multi.call_args_list]
    assert timeframes.count("1Day") == 1
    assert timeframes.count("1Min") == 3


def test_per_session_fetch_failure_skips_that_session():
    """A transient exception on one session's 1Min fetch must be skipped.

    The build must still complete with baselines for all symbols that have
    enough data from the remaining sessions. avg_or_volume_20d must reflect
    only the sessions that succeeded — denominator = 2, not 3 — so a wrong
    denominator returns 4000.0 while the correct one returns 6000.0.
    """
    call_count = {"n": 0}

    def _get_with_failure(syms, asset_class, timeframe, start, end):
        if timeframe == "1Day":
            return {
                s: [_daily(s, d, 100.0 + i, 400_000.0)
                    for i, d in enumerate(DAYS)]
                for s in ("AAA", "SPY")
            }
        # Fail the first per-session 1Min call; succeed for the other two.
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("transient network error")
        day = start.date()
        return {
            s: [_minute(s, day, m, 2_000.0) for m in range(3)]
            for s in ("AAA", "SPY")
        }

    data = MagicMock()
    data.get_bars_multi.side_effect = _get_with_failure
    out = build_baselines(data, ["AAA", "SPY"], NOW,
                          or_minutes=3, lookback_sessions=3, atr_window=2)
    # Build must still succeed despite one failed session.
    assert set(out) == {"AAA", "SPY"}
    # 2 successful sessions x (3 bars x 2000) = 6000 each; avg over 2 = 6000.0.
    # Wrong denominator (3 requested sessions) would give 4000.0.
    assert out["AAA"].avg_or_volume_20d == pytest.approx(6_000.0)
