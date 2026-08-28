# tests/test_opening_drive_scanner.py
import math

import pandas as pd
import pytest

from strategies.opening_drive_scanner import load_universe
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
