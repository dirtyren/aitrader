"""Regenerate config/universe_sp500_ndx100.csv.

The COMMITTED CSV is the source of truth. This script exists to refresh it
deliberately, not to be called at runtime -- a scanner that fetched
constituents live would make every backtest silently non-reproducible and
would change the universe underneath a running strategy.

Usage:
    python scripts/build_universe_sp500_ndx100.py --out config/universe_sp500_ndx100.csv

Requires network access and `pandas` + `lxml` for HTML table parsing.
Review the diff before committing: a constituent list that suddenly loses
100 names means the upstream page changed shape, not that the index did.

SOURCE NOTES (verified 2026-08-28):
- Wikipedia returns HTTP 403 to pandas' default User-Agent. All fetches use
  `requests` with an explicit User-Agent and pass the response text to
  pd.read_html via io.StringIO.
- The Nasdaq-100 URL in older references (https://en.wikipedia.org/wiki/Nasdaq-100)
  no longer carries a constituents table (18 tables, none with Ticker/Symbol).
  Use https://en.wikipedia.org/wiki/List_of_NASDAQ-100_companies instead:
  tables[0], 102 rows, columns: Ticker, Company, ICB Industry[1], ICB Subsector[1].
  Note the literal "[1]" suffix in the sector column name.
- S&P 500 source remains: https://en.wikipedia.org/wiki/List_of_S%26P_500_companies
  tables[0], ~503 rows, columns include Symbol and GICS Sector.
- ICB->GICS mapping: the two sources use different taxonomies. 15 Nasdaq-only
  symbols would carry ICB labels. Three ICB values are semantic duplicates of
  GICS names; without the mapping, SectorExposureFilter (Task 6) would treat
  "Technology" and "Information Technology" as different sectors and allow
  4 concurrent tech positions against a cap of 2. See _ICB_TO_GICS below.
"""
from __future__ import annotations

import argparse
import csv
import io
import sys

# Share-class tickers: Alpaca spells them with a DOT (BRK.B, BF.B), which is
# also how Wikipedia writes them. Do NOT normalize "." -> "-" (the Yahoo/Nasdaq
# convention): Alpaca rejects BRK-B with HTTP 400 "invalid symbol", and because
# one bad symbol fails the whole multi-symbol bars request, that error takes the
# entire scanner cut down with it — every session.
_SP500_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
_NDX_URL = "https://en.wikipedia.org/wiki/List_of_NASDAQ-100_companies"

# ICB -> GICS sector name normalisation.
# Three ICB values are semantic duplicates of GICS names; the other six ICB
# values already match GICS names exactly (Consumer Discretionary,
# Consumer Staples, Energy, Health Care, Industrials, Utilities) and are
# passed through unchanged.  Without this mapping SectorExposureFilter treats
# "Technology" and "Information Technology" as separate buckets and permits
# 4 concurrent tech positions against a cap of 2.
_ICB_TO_GICS: dict[str, str] = {
    "Technology": "Information Technology",
    "Telecommunications": "Communication Services",
    "Basic Materials": "Materials",
}

_UA = {"User-Agent": "aitrader-universe-builder/1.0 (research)"}


def _parse_sp500_df(df: "pd.DataFrame") -> dict[str, str]:
    """Map a raw S&P 500 Wikipedia DataFrame to symbol -> GICS sector.

    Extracted as a pure function so tests can drive it with a synthetic
    DataFrame without making any HTTP requests.
    """
    result: dict[str, str] = {}
    for _, row in df.iterrows():
        sym = str(row["Symbol"]).strip().upper()
        # str() on a pandas NaN cell produces "nan" which uppercases to "NAN"
        # -- truthy, so `if not sym` would pass it through as a real ticker.
        if sym in ("", "NAN"):
            continue
        result[sym] = str(row["GICS Sector"]).strip()
    return result


def _parse_ndx100_df(df: "pd.DataFrame") -> dict[str, str]:
    """Map a raw Nasdaq-100 Wikipedia DataFrame to symbol -> GICS sector.

    Extracted as a pure function so tests can drive it with a synthetic
    DataFrame without making any HTTP requests.

    The ICB Industry column is matched by prefix rather than exact name so
    that Wikipedia footnote renumbering (e.g. [1] -> [2]) does not break
    the parse silently. If no ICB Industry column exists at all, a
    RuntimeError is raised with the actual column list so the caller knows
    the page has changed shape.
    """
    # Find the ICB Industry column by prefix -- footnote number may change
    icb_col = next((c for c in df.columns if str(c).startswith("ICB Industry")), None)
    if icb_col is None:
        raise RuntimeError(
            f"Could not locate ICB Industry column in NDX table. "
            f"Columns found: {list(df.columns)}"
        )
    result: dict[str, str] = {}
    for _, row in df.iterrows():
        sym = str(row["Ticker"]).strip().upper()
        # str() on a pandas NaN cell produces "nan" which uppercases to "NAN"
        if sym in ("", "NAN"):
            continue
        icb_sector = str(row[icb_col]).strip()
        # Normalise ICB -> GICS; pass through already-matching names unchanged
        gics_sector = _ICB_TO_GICS.get(icb_sector, icb_sector)
        result[sym] = gics_sector
    return result


def _fetch_sp500() -> dict[str, str]:
    import pandas as pd
    import requests

    r = requests.get(_SP500_URL, headers=_UA, timeout=30)
    r.raise_for_status()
    tables = pd.read_html(io.StringIO(r.text))
    return _parse_sp500_df(tables[0])


def _fetch_ndx100() -> dict[str, str]:
    import pandas as pd
    import requests

    r = requests.get(_NDX_URL, headers=_UA, timeout=30)
    r.raise_for_status()
    tables = pd.read_html(io.StringIO(r.text))
    return _parse_ndx100_df(tables[0])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="config/universe_sp500_ndx100.csv")
    args = ap.parse_args()

    merged = _fetch_sp500()
    for sym, sector in _fetch_ndx100().items():
        merged.setdefault(sym, sector)     # S&P sector wins on overlap
    merged.pop("SPY", None)                # benchmark-only, never a candidate

    if len(merged) < 400:
        print(
            f"ERROR: only {len(merged)} symbols parsed; upstream page "
            f"likely changed shape. Refusing to write.",
            file=sys.stderr,
        )
        return 1

    with open(args.out, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["symbol", "sector"])
        for sym in sorted(merged):
            w.writerow([sym, merged[sym]])
    print(f"wrote {len(merged)} symbols to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
