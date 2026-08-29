# tests/test_opening_drive_scanner.py
import math
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from strategies.opening_drive_scanner import (
    OpeningDriveBaseline,
    OpeningDriveFilters,
    OpeningDriveScanner,
    OpeningRangeMetrics,
    ScanResult,
    baselines_are_stale,
    baselines_too_old_to_trade,
    compute_or_metrics,
    gate_reason,
    load_baselines,
    load_universe,
    or_return,
    rank_score,
    save_baselines,
)
from scripts.build_universe_sp500_ndx100 import _parse_ndx100_df, _parse_sp500_df
from core.bar import Bar


def test_load_universe_maps_symbol_to_sector(tmp_path):
    p = tmp_path / "u.csv"
    p.write_text("symbol,sector\nAAPL,Information Technology\nXOM,Energy\n")
    assert load_universe(p) == {
        "AAPL": "Information Technology",
        "XOM": "Energy",
    }


def test_load_universe_uppercases_and_skips_blanks(tmp_path):
    p = tmp_path / "u.csv"
    p.write_text("symbol,sector\naapl,Tech\n\n,\nmsft,Tech\n")
    assert load_universe(p) == {"AAPL": "Tech", "MSFT": "Tech"}


def test_load_universe_missing_sector_becomes_unknown(tmp_path):
    p = tmp_path / "u.csv"
    p.write_text("symbol,sector\nAAPL\n")
    assert load_universe(p) == {"AAPL": "UNKNOWN"}


def test_load_universe_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_universe(tmp_path / "nope.csv")


# ---------------------------------------------------------------------------
# Builder-script parsing helpers (no network, synthetic DataFrames)
# ---------------------------------------------------------------------------

def test_parse_sp500_df_basic():
    df = pd.DataFrame({"Symbol": ["AAPL", "XOM"], "GICS Sector": ["Information Technology", "Energy"]})
    assert _parse_sp500_df(df) == {"AAPL": "Information Technology", "XOM": "Energy"}


def test_parse_sp500_df_nan_ticker_skipped():
    """str(NaN) produces 'nan' -> uppercase 'NAN'; guard must exclude it."""
    df = pd.DataFrame(
        {"Symbol": ["AAPL", float("nan"), "MSFT"], "GICS Sector": ["Information Technology", "Energy", "Information Technology"]}
    )
    result = _parse_sp500_df(df)
    assert "NAN" not in result
    assert set(result.keys()) == {"AAPL", "MSFT"}


def test_parse_sp500_df_dot_normalised():
    df = pd.DataFrame({"Symbol": ["BRK.B"], "GICS Sector": ["Financials"]})
    assert _parse_sp500_df(df) == {"BRK-B": "Financials"}


def test_parse_ndx100_df_basic():
    df = pd.DataFrame(
        {"Ticker": ["AAPL", "AMZN"], "ICB Industry[1]": ["Technology", "Consumer Discretionary"]}
    )
    result = _parse_ndx100_df(df)
    # "Technology" must be normalised -> "Information Technology"
    assert result["AAPL"] == "Information Technology"
    assert result["AMZN"] == "Consumer Discretionary"


def test_parse_ndx100_df_nan_ticker_skipped():
    """str(NaN) produces 'nan' -> uppercase 'NAN'; guard must exclude it."""
    df = pd.DataFrame(
        {"Ticker": ["AAPL", float("nan"), "MSFT"], "ICB Industry[1]": ["Technology", "Technology", "Technology"]}
    )
    result = _parse_ndx100_df(df)
    assert "NAN" not in result
    assert set(result.keys()) == {"AAPL", "MSFT"}


def test_parse_ndx100_df_footnote_renumber():
    """ICB Industry[2] (footnote renumbered) must still resolve correctly."""
    df = pd.DataFrame(
        {"Ticker": ["AAPL"], "ICB Industry[2]": ["Telecommunications"]}
    )
    result = _parse_ndx100_df(df)
    # "Telecommunications" must be normalised -> "Communication Services"
    assert result["AAPL"] == "Communication Services"


def test_parse_ndx100_df_missing_icb_column_raises():
    """If no ICB Industry column exists at all, raise with column list."""
    df = pd.DataFrame({"Ticker": ["AAPL"], "Sector": ["Tech"]})
    with pytest.raises(RuntimeError, match="ICB Industry"):
        _parse_ndx100_df(df)


# ---------------------------------------------------------------------------
# Committed CSV integration test
# ---------------------------------------------------------------------------

def test_committed_universe_is_loadable_and_large():
    """The real CSV must exist, parse, and cover both indices."""
    u = load_universe("config/universe_sp500_ndx100.csv")
    assert len(u) > 400, f"universe too small: {len(u)}"
    assert "AAPL" in u and "MSFT" in u
    assert "SPY" not in u, "SPY is benchmark-only, never a universe member"
    assert all(v for v in u.values()), "every symbol needs a sector"


# ---------------------------------------------------------------------------
# Baseline model: persistence and staleness gates
# ---------------------------------------------------------------------------

NOW = datetime(2026, 8, 28, 14, 0, tzinfo=timezone.utc)


def _bl(days_old: float = 1.0, **kw) -> OpeningDriveBaseline:
    return OpeningDriveBaseline(
        atr_14d=kw.get("atr_14d", 3.0),
        avg_or_volume_20d=kw.get("avg_or_volume_20d", 50_000.0),
        avg_daily_volume_20d=kw.get("avg_daily_volume_20d", 400_000.0),
        computed_at=NOW - timedelta(days=days_old),
    )


def test_save_then_load_roundtrips(tmp_path):
    p = tmp_path / "b.json"
    save_baselines({"AAPL": _bl()}, p)
    out = load_baselines(p)
    assert out["AAPL"].atr_14d == 3.0
    assert out["AAPL"].avg_or_volume_20d == 50_000.0
    assert out["AAPL"].computed_at == NOW - timedelta(days=1)


def test_load_missing_file_returns_empty(tmp_path):
    assert load_baselines(tmp_path / "nope.json") == {}


def test_load_skips_malformed_entries_without_failing(tmp_path):
    p = tmp_path / "b.json"
    p.write_text('{"AAPL": {"atr_14d": 1.0}, "MSFT": {"atr_14d": 2.0,'
                 ' "avg_or_volume_20d": 1.0, "avg_daily_volume_20d": 1.0,'
                 ' "computed_at": "2026-08-27T14:00:00Z"}}')
    out = load_baselines(p)
    assert "AAPL" not in out       # missing required keys
    assert "MSFT" in out


def test_load_skips_null_computed_at_without_crashing(tmp_path):
    """Non-string computed_at (null) must be skipped, not crash the whole load."""
    p = tmp_path / "b.json"
    p.write_text('{"BAD": {"atr_14d": 1.0, "avg_or_volume_20d": 1.0,'
                 ' "avg_daily_volume_20d": 1.0, "computed_at": null},'
                 ' "GOOD": {"atr_14d": 2.0, "avg_or_volume_20d": 2.0,'
                 ' "avg_daily_volume_20d": 2.0, "computed_at": "2026-08-27T14:00:00Z"}}')
    out = load_baselines(p)
    assert "BAD" not in out
    assert "GOOD" in out


def test_load_skips_numeric_computed_at_without_crashing(tmp_path):
    """Non-string computed_at (number) must be skipped, not crash the whole load."""
    p = tmp_path / "b.json"
    p.write_text('{"BAD": {"atr_14d": 1.0, "avg_or_volume_20d": 1.0,'
                 ' "avg_daily_volume_20d": 1.0, "computed_at": 42},'
                 ' "GOOD": {"atr_14d": 2.0, "avg_or_volume_20d": 2.0,'
                 ' "avg_daily_volume_20d": 2.0, "computed_at": "2026-08-27T14:00:00Z"}}')
    out = load_baselines(p)
    assert "BAD" not in out
    assert "GOOD" in out


def test_empty_baselines_are_stale_and_untradeable():
    assert baselines_are_stale({}, NOW, 7) is True
    assert baselines_too_old_to_trade({}, NOW, 7) is True


def test_fresh_baselines_are_neither():
    b = {"AAPL": _bl(days_old=1)}
    assert baselines_are_stale(b, NOW, 7) is False
    assert baselines_too_old_to_trade(b, NOW, 7) is False


def test_stale_but_tradeable_between_max_and_2x_max():
    b = {"AAPL": _bl(days_old=10)}
    assert baselines_are_stale(b, NOW, 7) is True
    assert baselines_too_old_to_trade(b, NOW, 7) is False


def test_one_ancient_outlier_does_not_block_trading():
    """p95, not min: a single dead IEX symbol must not halt the universe."""
    b = {f"S{i}": _bl(days_old=1) for i in range(99)}
    b["DEAD"] = _bl(days_old=900)
    assert baselines_too_old_to_trade(b, NOW, 7) is False


def test_universally_ancient_baselines_block_trading():
    b = {f"S{i}": _bl(days_old=90) for i in range(100)}
    assert baselines_too_old_to_trade(b, NOW, 7) is True


# ---------------------------------------------------------------------------
# Task 4: opening-range metrics
# ---------------------------------------------------------------------------

def _bar(minute: int, o: float, h: float, l: float, c: float, v: float) -> Bar:
    return Bar(
        symbol="TEST",
        ts=datetime(2026, 8, 28, 13, 30 + minute, tzinfo=timezone.utc),
        open=o, high=h, low=l, close=c, volume=v,
    )


def _or_bars() -> list[Bar]:
    """3 bars: high 105, low 99, close 104, volume 30000."""
    return [
        _bar(0, 100.0, 102.0, 99.0, 101.0, 10_000),
        _bar(1, 101.0, 105.0, 100.5, 103.0, 12_000),
        _bar(2, 103.0, 104.5, 102.0, 104.0, 8_000),
    ]


def test_metrics_computed_from_bars():
    m = compute_or_metrics(
        "TEST", _or_bars(), _bl(avg_or_volume_20d=10_000.0, atr_14d=4.0),
        prev_close=100.0, spy_or_return=0.0, or_minutes=3,
    )
    assert m is not None
    assert m.or_high == 105.0
    assert m.or_low == 99.0
    assert m.or_close == 104.0
    assert m.or_volume == 30_000
    assert m.rvol_or == pytest.approx(3.0)          # 30000 / 10000
    assert m.disp_atr == pytest.approx(1.0)         # (104-100)/4
    assert m.or_width_atr == pytest.approx(1.5)     # (105-99)/4
    assert m.clv == pytest.approx((104 - 99) / 6)   # 0.8333
    assert m.bar_coverage == pytest.approx(1.0)     # 3 of 3 have volume


def test_rs_atr_is_relative_to_spy_in_atr_units():
    """Symbol +4% with SPY +1%, ATR 4% of price -> rs_atr = 0.75."""
    m = compute_or_metrics(
        "TEST", _or_bars(), _bl(avg_or_volume_20d=10_000.0, atr_14d=4.0),
        prev_close=100.0, spy_or_return=0.01, or_minutes=3,
    )
    assert m.rs_atr == pytest.approx((0.04 - 0.01) / 0.04)


def test_rs_atr_goes_negative_when_spy_outruns_symbol():
    m = compute_or_metrics(
        "TEST", _or_bars(), _bl(avg_or_volume_20d=10_000.0, atr_14d=4.0),
        prev_close=100.0, spy_or_return=0.10, or_minutes=3,
    )
    assert m.rs_atr < 0


def test_vwap_uses_typical_price_weighted_by_volume():
    bars = _or_bars()
    expected = (
        sum(b.typical_price * b.volume for b in bars)
        / sum(b.volume for b in bars)
    )
    m = compute_or_metrics(
        "TEST", bars, _bl(avg_or_volume_20d=10_000.0), prev_close=100.0,
        spy_or_return=0.0, or_minutes=3,
    )
    assert m.or_vwap == pytest.approx(expected)
    assert m.above_vwap is (104.0 > expected)


def test_bar_coverage_counts_only_bars_with_volume():
    bars = _or_bars() + [_bar(3, 104.0, 104.0, 104.0, 104.0, 0.0)]
    m = compute_or_metrics(
        "TEST", bars, _bl(avg_or_volume_20d=10_000.0), prev_close=100.0,
        spy_or_return=0.0, or_minutes=4,
    )
    assert m.bar_coverage == pytest.approx(0.75)     # 3 of 4


def test_bar_coverage_uses_or_minutes_not_bar_count():
    """A symbol IEX printed in only 3 of 30 minutes must score 0.1, not 1.0."""
    m = compute_or_metrics(
        "TEST", _or_bars(), _bl(avg_or_volume_20d=10_000.0), prev_close=100.0,
        spy_or_return=0.0, or_minutes=30,
    )
    assert m.bar_coverage == pytest.approx(0.1)


def test_flat_range_yields_zero_clv_not_a_crash():
    bars = [_bar(0, 100.0, 100.0, 100.0, 100.0, 5_000)]
    m = compute_or_metrics(
        "TEST", bars, _bl(avg_or_volume_20d=10_000.0), prev_close=100.0,
        spy_or_return=0.0, or_minutes=1,
    )
    assert m.clv == 0.0


def test_zero_volume_window_falls_back_to_close_for_vwap():
    bars = [_bar(0, 100.0, 101.0, 99.0, 100.5, 0.0)]
    m = compute_or_metrics(
        "TEST", bars, _bl(avg_or_volume_20d=10_000.0), prev_close=100.0,
        spy_or_return=0.0, or_minutes=1,
    )
    assert m.or_vwap == 100.5


@pytest.mark.parametrize("bars,prev_close,baseline_kw", [
    ([], 100.0, {}),                                    # no bars
    (None, 100.0, {}),                                   # no bars at all
    ("use_or_bars", 0.0, {}),                            # bad prev_close
    ("use_or_bars", 100.0, {"atr_14d": 0.0}),            # bad ATR
    ("use_or_bars", 100.0, {"avg_or_volume_20d": 0.0}),  # no volume baseline
])
def test_unusable_inputs_return_none(bars, prev_close, baseline_kw):
    if bars == "use_or_bars":
        bars = _or_bars()
    assert compute_or_metrics(
        "TEST", bars, _bl(**baseline_kw), prev_close=prev_close,
        spy_or_return=0.0, or_minutes=3,
    ) is None


def test_or_return_computes_fractional_return():
    assert or_return(_or_bars(), 100.0) == pytest.approx(0.04)


def test_or_return_none_on_bad_input():
    assert or_return([], 100.0) is None
    assert or_return(_or_bars(), 0.0) is None


# ---------------------------------------------------------------------------
# Task 5: gates, ranking, and run_cut
# ---------------------------------------------------------------------------

def _metrics(**kw) -> OpeningRangeMetrics:
    """A candidate that passes every gate unless overridden."""
    defaults = dict(
        symbol="TEST", or_high=105.0, or_low=99.0, or_close=104.0,
        or_volume=30_000.0, or_vwap=102.0, prev_close=100.0, atr_14d=4.0,
        rvol_or=3.0, disp_atr=1.0, or_width_atr=1.5, clv=0.83, rs_atr=0.75,
        above_vwap=True, bar_coverage=1.0,
    )
    defaults.update(kw)
    return OpeningRangeMetrics(**defaults)


F = OpeningDriveFilters()


def test_clean_candidate_passes_all_gates():
    assert gate_reason(_metrics(), _bl(), F) is None


@pytest.mark.parametrize("field,value,expected_gate", [
    ("or_close", 4.99, "min_price"),
    ("bar_coverage", 0.80, "min_bar_coverage"),
    ("rvol_or", 1.9, "min_rvol_or"),
    ("disp_atr", 0.4, "min_disp_atr"),
    ("or_width_atr", 0.39, "min_or_width_atr"),
    ("or_width_atr", 2.01, "max_or_width_atr"),
    ("clv", 0.59, "min_clv"),
    ("above_vwap", False, "above_vwap"),
    ("rs_atr", -0.01, "min_rs_atr"),
])
def test_each_gate_rejects_independently(field, value, expected_gate):
    """One test per gate: a candidate failing ONLY that gate is rejected
    naming that gate."""
    reason = gate_reason(_metrics(**{field: value}), _bl(), F)
    assert reason == expected_gate


def test_adv_gate_rejects_on_baseline_not_metrics():
    reason = gate_reason(_metrics(), _bl(avg_daily_volume_20d=99_999.0), F)
    assert reason == "min_avg_daily_volume"


def test_adv_gate_default_is_iex_denominated():
    """Regression guard: 1_000_000 (a consolidated-volume figure) would
    reject the entire universe on an IEX feed."""
    assert F.min_avg_daily_volume == 100_000.0


def test_negative_displacement_rejected_long_only():
    assert gate_reason(_metrics(disp_atr=-1.0), _bl(), F) == "min_disp_atr"


def test_rs_atr_gate_rejects_exactly_zero():
    """rs_atr == 0.0 is rejected: a symbol that merely matched the market
    does not qualify. The gate uses strictly-greater-than (<=) so that
    matching the benchmark does not count as outperforming it. A future
    editor 'fixing the inconsistency' to < would silently admit candidates
    that added no idiosyncratic return — the exact failure rs_atr exists to
    prevent."""
    assert gate_reason(_metrics(rs_atr=0.0), _bl(), F) == "min_rs_atr"


def test_rs_atr_gate_passes_small_positive():
    """Any outperformance above zero clears the gate."""
    assert gate_reason(_metrics(rs_atr=0.001), _bl(), F) is None


def test_rank_score_is_rvol_times_rs():
    assert rank_score(_metrics(rvol_or=3.0, rs_atr=0.5)) == pytest.approx(1.5)


# ── run_cut ────────────────────────────────────────────────────────────

def _scanner(**kw) -> OpeningDriveScanner:
    universe = {"AAA": "Tech", "BBB": "Tech", "CCC": "Energy"}
    baselines = {s: _bl(avg_or_volume_20d=10_000.0, atr_14d=4.0)
                 for s in list(universe) + ["SPY"]}
    return OpeningDriveScanner(
        universe=kw.get("universe", universe),
        baselines=kw.get("baselines", baselines),
        max_concurrent_positions=kw.get("max_concurrent_positions", 2),
        or_minutes=3,
    )


def _bars_for(close: float, volume: float = 30_000.0) -> list[Bar]:
    """3 one-minute bars ending at `close`, spanning a ~6-point range.

    The third bar's low is clamped to min(102.0, close) so the bar remains
    valid when close < 102.0 (e.g. SPY at 100.5). Bar.__post_init__ rejects
    low > close, which the brief's literal 102.0 would trigger.
    """
    return [
        _bar(0, 100.0, 102.0, 99.0, 101.0, volume / 3),
        _bar(1, 101.0, max(105.0, close + 1), 100.5, 103.0, volume / 3),
        _bar(2, 103.0, max(105.0, close + 1), min(102.0, close), close, volume / 3),
    ]


def test_run_cut_ranks_and_truncates_to_candidate_multiplier():
    s = _scanner()          # max_concurrent 2 * 1.5 -> top 3
    bars = {
        "AAA": _bars_for(104.0, 60_000),   # highest rvol
        "BBB": _bars_for(104.0, 30_000),
        "CCC": _bars_for(104.0, 20_000),
        "SPY": _bars_for(100.5, 30_000),
    }
    prev = {"AAA": 100.0, "BBB": 100.0, "CCC": 100.0, "SPY": 100.0}
    out = s.run_cut(bars, prev, NOW)
    assert [r.symbol for r in out] == ["AAA", "BBB", "CCC"]
    assert out[0].score > out[1].score > out[2].score
    assert all(isinstance(r, ScanResult) for r in out)
    assert out[0].sector == "Tech"
    assert out[0].side == "long"
    assert out[0].cut_ts == NOW


def test_run_cut_truncates_below_qualifier_count():
    s = _scanner(max_concurrent_positions=1)      # ceil(1*1.5) -> 2
    bars = {s_: _bars_for(104.0, 60_000 - i * 1000)
            for i, s_ in enumerate(["AAA", "BBB", "CCC"])}
    bars["SPY"] = _bars_for(100.5, 30_000)
    prev = {s_: 100.0 for s_ in ["AAA", "BBB", "CCC", "SPY"]}
    assert len(s.run_cut(bars, prev, NOW)) == 2


def test_run_cut_returns_empty_when_spy_bars_missing():
    """Load-bearing: no benchmark means no ranking, not an unbenchmarked one."""
    s = _scanner()
    bars = {"AAA": _bars_for(104.0), "BBB": _bars_for(104.0)}
    prev = {"AAA": 100.0, "BBB": 100.0, "SPY": 100.0}
    assert s.run_cut(bars, prev, NOW) == []


def test_run_cut_returns_empty_when_spy_prev_close_missing():
    s = _scanner()
    bars = {"AAA": _bars_for(104.0), "SPY": _bars_for(100.5)}
    assert s.run_cut(bars, {"AAA": 100.0}, NOW) == []


def test_run_cut_returns_empty_when_spy_coverage_too_low():
    s = _scanner()
    spy = [_bar(0, 100.0, 100.6, 99.9, 100.5, 0.0)]   # zero-volume bar
    bars = {"AAA": _bars_for(104.0), "SPY": spy}
    prev = {"AAA": 100.0, "SPY": 100.0}
    assert s.run_cut(bars, prev, NOW) == []


def test_run_cut_refuses_when_baselines_too_stale():
    s = _scanner(baselines={
        k: _bl(days_old=900, avg_or_volume_20d=10_000.0)
        for k in ["AAA", "BBB", "CCC", "SPY"]
    })
    bars = {"AAA": _bars_for(104.0), "SPY": _bars_for(100.5)}
    prev = {"AAA": 100.0, "SPY": 100.0}
    assert s.run_cut(bars, prev, NOW) == []


def test_run_cut_skips_symbols_without_baselines():
    s = _scanner(baselines={"SPY": _bl(avg_or_volume_20d=10_000.0),
                            "AAA": _bl(avg_or_volume_20d=10_000.0)})
    bars = {"AAA": _bars_for(104.0), "BBB": _bars_for(104.0),
            "SPY": _bars_for(100.5)}
    prev = {"AAA": 100.0, "BBB": 100.0, "SPY": 100.0}
    assert [r.symbol for r in s.run_cut(bars, prev, NOW)] == ["AAA"]


def test_run_cut_never_returns_spy_as_a_candidate():
    s = _scanner()
    bars = {"AAA": _bars_for(104.0), "SPY": _bars_for(120.0, 90_000)}
    prev = {"AAA": 100.0, "SPY": 100.0}
    assert "SPY" not in [r.symbol for r in s.run_cut(bars, prev, NOW)]


def test_run_cut_drops_candidates_weaker_than_spy():
    """AAA up 2.5% (disp_atr=0.75, clv=0.667 — passes all gates through
    above_vwap) while SPY is up 10% -> rs_atr = -1.75 -> gated out at
    min_rs_atr.

    Prior fixture used close=100.5 for AAA which was rejected at min_disp_atr
    (disp_atr=0.125 < 0.5) before reaching the rs_atr gate, so a broken
    rs_atr implementation would not have been caught.
    """
    s = _scanner()
    bars = {"AAA": _bars_for(103.0), "SPY": _bars_for(110.0)}
    prev = {"AAA": 100.0, "SPY": 100.0}
    assert s.run_cut(bars, prev, NOW) == []
