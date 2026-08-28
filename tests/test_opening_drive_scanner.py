# tests/test_opening_drive_scanner.py
import math
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from strategies.opening_drive_scanner import (
    OpeningDriveBaseline,
    baselines_are_stale,
    baselines_too_old_to_trade,
    load_baselines,
    load_universe,
    save_baselines,
)
from scripts.build_universe_sp500_ndx100 import _parse_ndx100_df, _parse_sp500_df


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
