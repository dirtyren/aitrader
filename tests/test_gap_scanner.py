"""Unit tests for the Gap-and-Go scanner."""
from __future__ import annotations
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from strategies.gap_scanner import (
    GapScanner,
    ScannerFilters,
    ScannerRanking,
    ScanResult,
    _Baseline,
)


_NOW = datetime(2026, 5, 29, 12, 30, tzinfo=timezone.utc)  # 08:30 ET


def _baseline(atr=2.0, premkt_vol=200_000, daily_vol=50_000_000,
              age_days: float = 0.0) -> _Baseline:
    return _Baseline(
        atr_14d=atr,
        avg_premarket_volume_20d=premkt_vol,
        avg_daily_volume_20d=daily_vol,
        computed_at=_NOW - timedelta(days=age_days),
    )


def _scanner(symbols=("AAPL", "MSFT", "TSLA"), baselines=None,
             filters=None, max_conc=4) -> GapScanner:
    return GapScanner(
        universe=list(symbols),
        baselines=baselines or {s: _baseline() for s in symbols},
        baselines_max_age_days=7,
        filters=filters or ScannerFilters(),
        ranking=ScannerRanking(candidate_multiplier=1.5),
        max_concurrent_positions=max_conc,
    )


def _snapshot(last=210.0, prev_close=200.0,
              h=212.0, l=199.0, v=2_000_000.0, vw=210.5) -> dict:
    return {
        "latestTrade": {"p": last},
        "minuteBar": {"h": h, "l": l, "v": v, "vw": vw, "c": last},
        "prevDailyBar": {"c": prev_close},
    }


# ---------------------------------------------------------------------------
# Universe + baselines I/O
# ---------------------------------------------------------------------------


def test_load_universe_handles_header_and_blank_lines(tmp_path: Path):
    p = tmp_path / "u.csv"
    p.write_text("symbol\nAAPL\n\nMSFT\n  TSLA  \n")
    assert GapScanner.load_universe(p) == ["AAPL", "MSFT", "TSLA"]


def test_load_universe_no_header(tmp_path: Path):
    p = tmp_path / "u.csv"
    p.write_text("AAPL\nMSFT\n")
    assert GapScanner.load_universe(p) == ["AAPL", "MSFT"]


def test_load_universe_missing_file_raises(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        GapScanner.load_universe(tmp_path / "missing.csv")


def test_save_and_load_baselines_roundtrip(tmp_path: Path):
    p = tmp_path / "baselines.json"
    bls = {
        "AAPL": _baseline(atr=3.42, premkt_vol=850_000, daily_vol=52_400_000),
        "MSFT": _baseline(atr=2.10, premkt_vol=400_000, daily_vol=30_000_000),
    }
    GapScanner.save_baselines(bls, p)
    loaded = GapScanner.load_baselines(p)
    assert set(loaded.keys()) == {"AAPL", "MSFT"}
    assert loaded["AAPL"].atr_14d == 3.42
    assert loaded["MSFT"].avg_daily_volume_20d == 30_000_000


def test_load_baselines_missing_returns_empty(tmp_path: Path):
    assert GapScanner.load_baselines(tmp_path / "missing.json") == {}


def test_load_baselines_corrupt_returns_empty(tmp_path: Path):
    p = tmp_path / "bad.json"
    p.write_text("{ this is not json")
    assert GapScanner.load_baselines(p) == {}


# ---------------------------------------------------------------------------
# Baseline staleness
# ---------------------------------------------------------------------------


def test_baselines_age_days_uses_oldest_entry():
    sc = _scanner(symbols=("AAPL", "MSFT"),
                  baselines={
                      "AAPL": _baseline(age_days=1.0),
                      "MSFT": _baseline(age_days=10.0),
                  })
    assert sc.baselines_age_days(_NOW) == pytest.approx(10.0)


def test_baselines_stale_at_threshold():
    sc = _scanner(baselines={"AAPL": _baseline(age_days=8.0)},
                  symbols=("AAPL",))
    assert sc.baselines_are_stale(_NOW) is True
    assert sc.baselines_too_old_to_trade(_NOW) is False  # 8 < 14


def test_baselines_too_old_to_trade_at_double_threshold():
    sc = _scanner(baselines={"AAPL": _baseline(age_days=15.0)},
                  symbols=("AAPL",))
    assert sc.baselines_too_old_to_trade(_NOW) is True


def test_run_cut_refuses_when_baselines_too_stale():
    sc = _scanner(baselines={"AAPL": _baseline(age_days=15.0)},
                  symbols=("AAPL",))
    sc.candidate_status({"AAPL": _snapshot()}, _NOW)
    assert sc.run_cut(_NOW) == []


# ---------------------------------------------------------------------------
# Snapshot tracking
# ---------------------------------------------------------------------------


def test_candidate_status_accumulates_volume_and_extremes():
    sc = _scanner(symbols=("AAPL",))
    sc.candidate_status({"AAPL": _snapshot(h=212.0, l=199.0, v=500_000.0)}, _NOW)
    sc.candidate_status({"AAPL": _snapshot(h=215.0, l=198.0, v=700_000.0)},
                        _NOW + timedelta(minutes=5))
    state = sc.get_state("AAPL")
    assert state.premarket_high == 215.0
    assert state.premarket_low == 198.0
    assert state.premarket_volume == 1_200_000.0


def test_candidate_status_ignores_unknown_symbols():
    sc = _scanner(symbols=("AAPL",))
    sc.candidate_status({"GOOGL": _snapshot()}, _NOW)
    assert sc.get_state("AAPL").premarket_volume == 0.0


def test_candidate_status_handles_partial_response():
    """Snapshot endpoint may omit prevDailyBar when stock is freshly listed."""
    sc = _scanner(symbols=("AAPL",))
    sc.candidate_status({"AAPL": {"latestTrade": {"p": 210.0},
                                  "minuteBar": {"h": 211, "l": 209, "v": 1000}}},
                        _NOW)
    state = sc.get_state("AAPL")
    assert state.last_price == 210.0
    assert state.prev_close == 0.0
    assert state.premarket_volume == 1000.0


# ---------------------------------------------------------------------------
# Cut filters
# ---------------------------------------------------------------------------


def test_run_cut_happy_path_returns_long_candidate():
    sc = _scanner(symbols=("AAPL",))
    # Gap +5%, RVOL 10x, gap_atr_mult = 10/2 = 5.0 — easily passes.
    sc.candidate_status({"AAPL": _snapshot(last=210.0, prev_close=200.0,
                                           v=2_000_000.0)}, _NOW)
    results = sc.run_cut(_NOW)
    assert len(results) == 1
    r = results[0]
    assert r.symbol == "AAPL"
    assert r.side == "long"
    assert r.gap_pct == pytest.approx(5.0)
    assert r.gap_atr_mult == pytest.approx(5.0)
    assert r.rvol == pytest.approx(10.0)


def test_run_cut_rejects_negative_gap_long_only():
    sc = _scanner(symbols=("AAPL",))
    sc.candidate_status({"AAPL": _snapshot(last=190.0, prev_close=200.0,
                                           v=2_000_000.0)}, _NOW)
    assert sc.run_cut(_NOW) == []


@pytest.mark.parametrize("filters_kwargs,snap_kwargs,reason", [
    (dict(min_price=10.0), dict(last=8.0, prev_close=7.5, v=2_000_000), "low price"),
    (dict(min_avg_daily_volume=100_000_000.0), dict(v=2_000_000), "low ADV"),
    (dict(min_rvol=20.0), dict(v=2_000_000), "low RVOL"),
    (dict(min_gap_pct=10.0), dict(last=205.0, prev_close=200.0, v=2_000_000), "small gap pct"),
    (dict(min_gap_atr_mult=5.0), dict(last=202.0, prev_close=200.0, v=2_000_000), "small gap atr"),
])
def test_run_cut_filter_rejections(filters_kwargs, snap_kwargs, reason):
    sc = _scanner(symbols=("AAPL",), filters=ScannerFilters(**filters_kwargs))
    sc.candidate_status({"AAPL": _snapshot(**snap_kwargs)}, _NOW)
    assert sc.run_cut(_NOW) == [], f"expected reject: {reason}"


def test_run_cut_at_exactly_threshold_passes():
    """Filters use strict comparisons ('>=' for floors, '>' for gap signs)."""
    sc = _scanner(
        symbols=("AAPL",),
        filters=ScannerFilters(
            min_price=5.0, min_avg_daily_volume=1_000_000.0, min_rvol=5.0,
            min_gap_pct=4.0, min_gap_atr_mult=1.5,
        ),
    )
    # Gap exactly +4%, RVOL exactly 5x, gap_atr_mult = 8/2 = 4.0 (>1.5).
    sc.candidate_status({"AAPL": _snapshot(last=208.0, prev_close=200.0,
                                           v=1_000_000.0)}, _NOW)
    results = sc.run_cut(_NOW)
    assert len(results) == 1


def test_run_cut_drops_symbol_missing_baseline():
    sc = _scanner(symbols=("AAPL", "MSFT"),
                  baselines={"AAPL": _baseline()})  # MSFT missing
    sc.candidate_status({"AAPL": _snapshot(), "MSFT": _snapshot()}, _NOW)
    results = sc.run_cut(_NOW)
    assert [r.symbol for r in results] == ["AAPL"]


def test_run_cut_drops_symbol_missing_snapshot():
    sc = _scanner(symbols=("AAPL", "MSFT"))
    sc.candidate_status({"AAPL": _snapshot()}, _NOW)
    results = sc.run_cut(_NOW)
    assert [r.symbol for r in results] == ["AAPL"]


# ---------------------------------------------------------------------------
# Ranking + top-N truncation
# ---------------------------------------------------------------------------


def test_run_cut_ranks_by_gap_atr_mult_times_rvol_desc():
    sc = _scanner(symbols=("AAA", "BBB", "CCC"),
                  baselines={s: _baseline() for s in ("AAA", "BBB", "CCC")})
    # Same gap pct, different rvol → ranking by gap_atr_mult * rvol.
    sc.candidate_status({
        "AAA": _snapshot(last=210, prev_close=200, v=1_000_000),  # rvol 5
        "BBB": _snapshot(last=210, prev_close=200, v=4_000_000),  # rvol 20
        "CCC": _snapshot(last=210, prev_close=200, v=2_000_000),  # rvol 10
    }, _NOW)
    results = sc.run_cut(_NOW)
    assert [r.symbol for r in results] == ["BBB", "CCC", "AAA"]


def test_run_cut_truncates_to_max_conc_times_multiplier():
    syms = tuple(f"S{i}" for i in range(10))
    sc = _scanner(symbols=syms,
                  baselines={s: _baseline() for s in syms},
                  max_conc=4)  # top_N = ceil(4 * 1.5) = 6
    # All candidates pass. Order by gap_pct so we can predict the truncated set.
    # Each candidate gaps further (10 + 2*i %) so all pass the 4% min.
    for i, s in enumerate(syms):
        last = 200.0 * (1.0 + 0.10 + 0.02 * i)  # 10%, 12%, 14%, ... 28%
        sc.candidate_status(
            {s: _snapshot(last=last, prev_close=200, v=2_000_000)},
            _NOW,
        )
    results = sc.run_cut(_NOW)
    assert len(results) == 6
    # Top 6 should be the symbols with the largest gap (i = 4..9 → S4..S9).
    assert {r.symbol for r in results} == {"S4", "S5", "S6", "S7", "S8", "S9"}


def test_run_cut_returns_at_least_one_when_anything_passes():
    """ceil(max_conc * multiplier) is always >= 1, even at max_conc=0."""
    sc = _scanner(symbols=("AAPL",), max_conc=0)
    sc.candidate_status({"AAPL": _snapshot()}, _NOW)
    results = sc.run_cut(_NOW)
    assert len(results) == 1


# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------


def test_construction_rejects_empty_universe():
    with pytest.raises(ValueError, match="empty"):
        GapScanner(universe=[], baselines={})


def test_reset_for_new_day_clears_state():
    sc = _scanner(symbols=("AAPL",))
    sc.candidate_status({"AAPL": _snapshot()}, _NOW)
    assert sc.get_state("AAPL").premarket_volume > 0
    sc.reset_for_new_day()
    assert sc.get_state("AAPL").premarket_volume == 0


def test_run_cut_attaches_cut_ts():
    sc = _scanner(symbols=("AAPL",))
    sc.candidate_status({"AAPL": _snapshot()}, _NOW)
    [r] = sc.run_cut(_NOW)
    assert r.cut_ts == _NOW
