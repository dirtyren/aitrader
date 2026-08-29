"""The universe file must contain symbols this broker actually accepts.

Regression guard for a defect that would have stopped the strategy trading on
every single session. The universe builder normalized share-class tickers from
`BRK.B` to `BRK-B` (the Yahoo/Nasdaq convention). Alpaca uses the dot, and
rejects the dash with HTTP 400 "invalid symbol".

That matters far more than two bad rows suggest: the scanner fetches its
opening range with ONE multi-symbol request covering the whole universe, so a
single unrecognized symbol fails the entire request. `run_cut` does not catch
it, so the cut raises, `run_day` retries in a loop, and the strategy never
trades — while every config and test looks correct.

These tests are offline: they assert the file's shape, not live broker state,
so they run in CI without credentials.
"""
from __future__ import annotations

import csv
import re
from pathlib import Path

import pytest

UNIVERSE = Path(__file__).resolve().parents[1] / "config" / "universe_sp500_ndx100.csv"

# Alpaca us_equity symbols: uppercase alphanumerics, optionally ONE dot-suffixed
# share class (BRK.B, BF.B). A dash is never valid in this namespace.
ALPACA_SYMBOL = re.compile(r"^[A-Z0-9]{1,6}(\.[A-Z])?$")


def _rows() -> list[tuple[str, str]]:
    with UNIVERSE.open() as f:
        reader = csv.reader(f)
        next(reader)  # header
        return [(r[0].strip(), r[1].strip()) for r in reader if r and r[0].strip()]


def test_universe_is_populated():
    """Guard the guard — an empty read would make everything below vacuous."""
    assert len(_rows()) > 400, f"universe unexpectedly small: {len(_rows())}"


@pytest.mark.parametrize("symbol", [s for s, _ in _rows()])
def test_symbol_matches_alpaca_format(symbol):
    assert ALPACA_SYMBOL.match(symbol), (
        f"{symbol!r} is not a valid Alpaca us_equity symbol"
    )


def test_no_dash_share_classes():
    """The exact defect: BRK-B / BF-B instead of BRK.B / BF.B."""
    dashed = [s for s, _ in _rows() if "-" in s]
    assert not dashed, (
        f"dash-normalized share classes present: {dashed}. Alpaca rejects these "
        f"with HTTP 400, and one bad symbol fails the whole universe bars request."
    )


def test_known_share_classes_use_dots():
    symbols = {s for s, _ in _rows()}
    for expected in ("BRK.B", "BF.B"):
        assert expected in symbols, f"expected {expected} in the universe"


def test_benchmark_is_not_a_universe_member():
    """SPY is the benchmark leg of rs_atr and must never be a candidate."""
    assert "SPY" not in {s for s, _ in _rows()}


def test_no_duplicate_symbols():
    symbols = [s for s, _ in _rows()]
    dupes = {s for s in symbols if symbols.count(s) > 1}
    assert not dupes, f"duplicate symbols: {sorted(dupes)}"


def test_every_symbol_has_a_sector():
    """SectorExposureFilter buckets a blank sector as UNKNOWN, which would
    silently merge unrelated names into one concentration bucket."""
    missing = [s for s, sec in _rows() if not sec]
    assert not missing, f"symbols with no sector: {missing}"
