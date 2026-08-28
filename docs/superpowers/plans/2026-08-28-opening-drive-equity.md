# Opening Drive Equity Strategy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a screened intraday momentum strategy that ranks S&P 500 + Nasdaq-100 on the 09:30–10:00 opening range, enters top candidates on a confirmed continuation trigger between 10:00–11:00, and is flat by 15:30.

**Architecture:** A pure-logic scanner (`OpeningDriveScanner`) computes self-normalized opening-range metrics from bulk-fetched bars and applies a gate cascade plus two-factor ranking. A per-symbol setup state machine (`OpeningDriveSetup`) waits for an OR-high reclaim with volume confirmation. A phase-based orchestrator (`OpeningDriveLoop`) drives the daily lifecycle. Every phase method is callable without network or sleep, following the `gap_and_go` pattern.

**Tech Stack:** Python 3.10+, `requests` (Alpaca REST), `pytz`, `pyyaml`, `pytest`, Docker Compose, Streamlit (dashboard, no changes needed).

**Spec:** `docs/superpowers/specs/2026-08-28-opening-drive-equity-design.md`

## Global Constraints

Every task's requirements implicitly include this section. Values are copied verbatim from the spec.

- **Market data feed is IEX only.** `broker/alpaca_client.py` hardcodes `"feed": "iex"`. Every screening metric MUST be self-normalized (symbol vs. its own history, or a ratio within one feed). Never compare one symbol's raw IEX volume to another's.
- **`avg_daily_volume_20d` gate is `100_000` and is IEX-denominated, not consolidated.** Do not copy `gap_and_go`'s `1_000_000` — against IEX volume that rejects the entire universe silently.
- **Long-only in v1.** Negative `disp_atr` candidates are dropped, never inverted.
- **Universe:** S&P 500 + Nasdaq-100, ~515 unique symbols. **SPY is appended to every bulk request as benchmark-only** and is never a candidate.
- **If SPY data is missing or fails `bar_coverage`, `run_cut` MUST return an empty list.** Never fall back to an unbenchmarked ranking.
- **Strategy name:** `opening_drive_equity_trader`. **Setup name:** `opening_drive`.
- **Risk:** `max_concurrent_positions: 5`, `max_risk_per_trade: 0.005`, `max_notional_per_trade_pct: 0.07`, `max_per_sector: 2`.
- **`ConsecutiveLossFilter` scope MUST be `system_wide`**, not `per_symbol` — this strategy rotates symbols daily. ⚠️ The spec says `per_strategy`; **that scope does not exist in the code.** `ConsecutiveLossFilter.check` branches on the exact string `"system_wide"` and treats every other value as per-symbol, so `per_strategy` would silently fall through and never fire. `system_wide` delivers the spec's intent.
- **Times are America/New_York; all internal timestamps are timezone-aware UTC.** Cut 10:00, entry window ends 11:00, flatten 15:30, baseline refresh 16:10.
- **`max_hold_bars: 36`** on 5-min bars, counted from the first managed-phase bar at 11:00, not from entry.
- **The loop MUST call `RiskManager.update_cash`** on every equity refresh. `main_gap_and_go.py` omits this; do not copy that omission.
- **The 15:30 flatten MUST cancel live OCO child orders before submitting the market close.** `OrderExecutor.close_position` does not do this.

---

## File Structure

| Path | Responsibility |
|---|---|
| `broker/alpaca_client.py` | **Modify** — add `get_stock_bars_multi()` |
| `broker/alpaca_data.py` | **Modify** — add `get_bars_multi()` |
| `strategies/opening_drive_scanner.py` | **Create** — baselines model + IO, `compute_or_metrics()`, gates, ranking, `OpeningDriveScanner` |
| `strategies/setup_opening_drive.py` | **Create** — `OpeningDriveSetup` trigger state machine |
| `scheduler/opening_drive_loop.py` | **Create** — `OpeningDriveConfig`, `OpeningDriveLoop` phase handlers |
| `risk/filters.py` | **Modify** — add `SectorExposureFilter` |
| `risk/pdt_guard.py` | **Create** — PDT precondition check |
| `main_opening_drive.py` | **Create** — production wiring |
| `config/settings_opening_drive_equity.yaml` | **Create** — strategy config |
| `config/universe_sp500_ndx100.csv` | **Create** — `symbol,sector` |
| `scripts/build_universe_sp500_ndx100.py` | **Create** — universe CSV builder |
| `scripts/build_opening_drive_baselines.py` | **Create** — 16:10 baseline job entry point |
| `config/settings_sma_slope_equity.yaml` | **Modify** — notional cap 0.95 → 0.60 |
| `docker-compose.yml` | **Modify** — add `trader-opening-drive-equity` |

Tests: `tests/test_alpaca_bars_multi.py`, `tests/test_opening_drive_scanner.py`, `tests/test_opening_drive_setup.py`, `tests/test_opening_drive_loop.py`, `tests/test_sector_exposure_filter.py`, `tests/test_pdt_guard.py`, `tests/test_dashboard_dynamic_symbols.py`.

---

### Task 1: Bulk multi-symbol bars path

**Files:**
- Modify: `broker/alpaca_client.py` (add method after `get_stock_bars`, which ends at line 471)
- Modify: `broker/alpaca_data.py` (add method after `get_bars`)
- Test: `tests/test_alpaca_bars_multi.py`

**Interfaces:**
- Consumes: `AlpacaClient._data_request`, `_MAX_BAR_PAGES`, `_bars_from_raw` (all existing)
- Produces:
  - `AlpacaClient.get_stock_bars_multi(symbols: list[str], timeframe: str, start: datetime, end: datetime, limit: int = 10000, chunk_size: int = 200) -> dict[str, list[dict]]`
  - `AlpacaData.get_bars_multi(symbols: list[str], asset_class: str, timeframe: str, start: datetime, end: datetime) -> dict[str, list[Bar]]`

**Context you need:** The single-symbol endpoint `/v2/stocks/{symbol}/bars` returns `{"bars": [...]}` — a **list**. The multi-symbol endpoint `/v2/stocks/bars?symbols=A,B` returns `{"bars": {"A": [...], "B": [...]}}` — a **dict keyed by symbol**. This difference is the main source of bugs here. Pagination works the same way (`next_page_token`) but pages must be merged per symbol.

`get_bars_multi` deliberately does **not** cache. `AlpacaData.get_bars` caches by `(symbol, timeframe, start, end)`, which is right for historical windows but wrong for a same-day intraday window — a re-run within the same session would serve a stale partial opening range. The baseline builder gets its caching from `baselines.json` instead.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_alpaca_bars_multi.py
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from broker.alpaca_client import AlpacaClient


def _resp(payload: dict) -> MagicMock:
    r = MagicMock()
    r.json.return_value = payload
    return r


def _client() -> AlpacaClient:
    c = AlpacaClient.__new__(AlpacaClient)  # bypass __init__ (needs credentials)
    c._data_request = MagicMock()
    return c


START = datetime(2026, 8, 28, 13, 30, tzinfo=timezone.utc)
END = datetime(2026, 8, 28, 14, 0, tzinfo=timezone.utc)


def test_returns_dict_keyed_by_symbol():
    c = _client()
    c._data_request.return_value = _resp({
        "bars": {
            "AAPL": [{"t": "2026-08-28T13:30:00Z", "o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 100}],
            "MSFT": [{"t": "2026-08-28T13:30:00Z", "o": 3, "h": 4, "l": 2.5, "c": 3.5, "v": 200}],
        },
    })
    out = c.get_stock_bars_multi(["AAPL", "MSFT"], "1Min", START, END)
    assert set(out) == {"AAPL", "MSFT"}
    assert out["AAPL"][0]["v"] == 100


def test_merges_pages_per_symbol():
    c = _client()
    c._data_request.side_effect = [
        _resp({"bars": {"AAPL": [{"v": 1}]}, "next_page_token": "tok"}),
        _resp({"bars": {"AAPL": [{"v": 2}], "MSFT": [{"v": 3}]}}),
    ]
    out = c.get_stock_bars_multi(["AAPL", "MSFT"], "1Min", START, END)
    assert [b["v"] for b in out["AAPL"]] == [1, 2]
    assert [b["v"] for b in out["MSFT"]] == [3]


def test_chunks_symbol_list():
    c = _client()
    c._data_request.return_value = _resp({"bars": {}})
    c.get_stock_bars_multi(["A", "B", "C"], "1Min", START, END, chunk_size=2)
    assert c._data_request.call_count == 2
    first = c._data_request.call_args_list[0].kwargs["params"]["symbols"]
    second = c._data_request.call_args_list[1].kwargs["params"]["symbols"]
    assert first == "A,B"
    assert second == "C"


def test_page_token_does_not_leak_between_chunks():
    c = _client()
    c._data_request.side_effect = [
        _resp({"bars": {"A": [{"v": 1}]}, "next_page_token": "tok"}),
        _resp({"bars": {"B": [{"v": 2}]}}),
        _resp({"bars": {"C": [{"v": 3}]}}),
    ]
    c.get_stock_bars_multi(["A", "B", "C"], "1Min", START, END, chunk_size=2)
    third = c._data_request.call_args_list[2].kwargs["params"]
    assert "page_token" not in third


def test_empty_symbols_short_circuits():
    c = _client()
    assert c.get_stock_bars_multi([], "1Min", START, END) == {}
    c._data_request.assert_not_called()


def test_naive_datetime_rejected():
    c = _client()
    with pytest.raises(ValueError, match="timezone-aware"):
        c.get_stock_bars_multi(["AAPL"], "1Min", datetime(2026, 8, 28, 13, 30), END)


def test_uses_iex_feed():
    c = _client()
    c._data_request.return_value = _resp({"bars": {}})
    c.get_stock_bars_multi(["AAPL"], "1Min", START, END)
    assert c._data_request.call_args.kwargs["params"]["feed"] == "iex"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_alpaca_bars_multi.py -v`
Expected: FAIL with `AttributeError: 'AlpacaClient' object has no attribute 'get_stock_bars_multi'`

- [ ] **Step 3: Implement `get_stock_bars_multi`**

Add to `broker/alpaca_client.py`, directly after the `get_stock_bars` method:

```python
    def get_stock_bars_multi(self, symbols: list[str], timeframe: str,
                            start: datetime, end: datetime,
                            limit: int = 10000,
                            chunk_size: int = 200) -> dict[str, list[dict]]:
        """GET /v2/stocks/bars — multi-symbol bars keyed by symbol.

        Unlike the single-symbol endpoint (which returns ``{"bars": [...]}``),
        this returns ``{"bars": {"AAPL": [...], ...}}``. Pages are merged
        per symbol.

        The symbol list is chunked so a 500-symbol universe does not produce
        an unreasonably long query string. Each chunk paginates independently;
        ``page_token`` must not leak across chunks, which is why ``params`` is
        rebuilt per chunk.
        """
        if start.tzinfo is None or end.tzinfo is None:
            raise ValueError("start and end must be timezone-aware")
        if not symbols:
            return {}

        out: dict[str, list[dict]] = {}
        for i in range(0, len(symbols), chunk_size):
            chunk = symbols[i:i + chunk_size]
            params = {
                "symbols": ",".join(chunk),
                "timeframe": timeframe,
                "start": start.isoformat().replace("+00:00", "Z"),
                "end": end.isoformat().replace("+00:00", "Z"),
                "limit": limit,
                "adjustment": "raw",
                "feed": "iex",
            }
            for _ in range(_MAX_BAR_PAGES):
                response = self._data_request(
                    "GET", "/v2/stocks/bars", params=params,
                )
                body = response.json()
                for sym, bars in (body.get("bars") or {}).items():
                    out.setdefault(sym, []).extend(bars or [])
                token = body.get("next_page_token")
                if not token:
                    break
                params["page_token"] = token
        return out
```

- [ ] **Step 4: Implement `get_bars_multi`**

Add to `broker/alpaca_data.py`, directly after the `get_bars` method:

```python
    def get_bars_multi(
        self,
        symbols: list[str],
        asset_class: str,
        timeframe: str,
        start: datetime,
        end: datetime,
    ) -> dict[str, list[Bar]]:
        """Bulk multi-symbol bars, parsed into Bar objects, keyed by symbol.

        Deliberately uncached: the caller uses this for a same-day intraday
        window, where the disk cache in get_bars() would serve a stale
        partial range on a re-run within the same session.

        Symbols absent from the response (no prints in the window) are simply
        absent from the returned dict — callers must handle missing keys
        rather than assume one entry per requested symbol.
        """
        if asset_class != "equity":
            raise ValueError(
                f"get_bars_multi supports equity only, got {asset_class!r}"
            )
        raw_by_symbol = self.client.get_stock_bars_multi(
            symbols, timeframe, start, end,
        )
        return {
            sym: _bars_from_raw(raw, sym)
            for sym, raw in raw_by_symbol.items()
        }
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_alpaca_bars_multi.py -v`
Expected: 7 passed

- [ ] **Step 6: Run the full suite to confirm nothing regressed**

Run: `pytest -q`
Expected: no new failures versus the pre-task baseline

- [ ] **Step 7: Commit**

```bash
git add broker/alpaca_client.py broker/alpaca_data.py tests/test_alpaca_bars_multi.py
git commit -m "feat(broker): multi-symbol bulk bars fetch

Adds get_stock_bars_multi (client) and get_bars_multi (data layer).
The multi-symbol endpoint returns bars keyed by symbol rather than as a
list, and pages must be merged per symbol. Symbol lists are chunked so a
500-symbol universe does not build an oversized query string, with
page_token scoped per chunk.

Deliberately uncached: the consumer reads a same-day intraday window,
where get_bars()'s disk cache would serve a stale partial range."
```

---

### Task 2: Universe CSV with sector map

**Files:**
- Create: `scripts/build_universe_sp500_ndx100.py`
- Create: `config/universe_sp500_ndx100.csv`
- Test: `tests/test_opening_drive_scanner.py` (universe-loading tests only; later tasks append to this file)

**Interfaces:**
- Produces:
  - `load_universe(path) -> dict[str, str]` — maps symbol → sector. Lives in `strategies/opening_drive_scanner.py` (created here as the module's first content).

**Context you need:** `GapScanner.load_universe` returns a bare `list[str]`. This strategy needs sectors for `SectorExposureFilter`, so the loader returns a dict instead. The CSV has a header row `symbol,sector`.

Constituent lists change over time. The builder script is a convenience for regenerating the CSV; the **committed CSV is the source of truth** for reproducible backtests. Do not have the scanner fetch constituents at runtime — that would make every backtest silently non-reproducible.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_opening_drive_scanner.py
import pytest

from strategies.opening_drive_scanner import load_universe


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


def test_committed_universe_is_loadable_and_large():
    """The real CSV must exist, parse, and cover both indices."""
    u = load_universe("config/universe_sp500_ndx100.csv")
    assert len(u) > 400, f"universe too small: {len(u)}"
    assert "AAPL" in u and "MSFT" in u
    assert "SPY" not in u, "SPY is benchmark-only, never a universe member"
    assert all(v for v in u.values()), "every symbol needs a sector"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_opening_drive_scanner.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'strategies.opening_drive_scanner'`

- [ ] **Step 3: Create the module with the loader**

Create `strategies/opening_drive_scanner.py`:

```python
"""Opening Drive scanner: universe, baselines, metrics, gates, ranking.

Screens the S&P 500 + Nasdaq-100 on the 09:30-10:00 opening range and
returns the day's ranked watchlist.

All metrics are self-normalized (symbol vs. its own trailing history, or a
ratio taken within one feed) because the market-data feed is IEX-only,
carrying roughly 2% of consolidated volume. Absolute cross-sectional
comparisons between symbols are invalid on this feed; ratios are not.

Split into pure functions plus a stateful holder so tests drive metrics,
gates, and ranking without any network access.
"""
from __future__ import annotations

import csv
import json
import logging
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from core.bar import Bar

logger = logging.getLogger(__name__)


def load_universe(path: str | Path) -> dict[str, str]:
    """Read `symbol,sector` CSV into a symbol -> sector mapping.

    Returns a dict rather than GapScanner's list because SectorExposureFilter
    needs the sector for every candidate. Symbols with no sector column get
    "UNKNOWN", which the sector cap then treats as its own bucket.
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Universe file not found: {p}")
    out: dict[str, str] = {}
    with p.open() as f:
        for i, row in enumerate(csv.reader(f)):
            if not row:
                continue
            symbol = row[0].strip().upper()
            if not symbol:
                continue
            if i == 0 and symbol == "SYMBOL":
                continue
            sector = row[1].strip() if len(row) > 1 and row[1].strip() else "UNKNOWN"
            out[symbol] = sector
    return out
```

- [ ] **Step 4: Write the universe builder script**

Create `scripts/build_universe_sp500_ndx100.py`:

```python
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
"""
from __future__ import annotations

import argparse
import csv
import sys

_SP500_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
_NDX_URL = "https://en.wikipedia.org/wiki/Nasdaq-100"


def _fetch_sp500() -> dict[str, str]:
    import pandas as pd
    tables = pd.read_html(_SP500_URL)
    df = tables[0]
    return {
        str(r["Symbol"]).strip().upper().replace(".", "-"): str(r["GICS Sector"]).strip()
        for _, r in df.iterrows()
    }


def _fetch_ndx100() -> dict[str, str]:
    import pandas as pd
    tables = pd.read_html(_NDX_URL)
    for df in tables:
        cols = {str(c) for c in df.columns}
        if "Ticker" in cols or "Symbol" in cols:
            sym_col = "Ticker" if "Ticker" in cols else "Symbol"
            sec_col = next(
                (c for c in ("GICS Sector", "Sector") if c in cols), None,
            )
            return {
                str(r[sym_col]).strip().upper().replace(".", "-"):
                    (str(r[sec_col]).strip() if sec_col else "UNKNOWN")
                for _, r in df.iterrows()
            }
    raise RuntimeError("Could not locate the Nasdaq-100 constituents table")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="config/universe_sp500_ndx100.csv")
    args = ap.parse_args()

    merged = _fetch_sp500()
    for sym, sector in _fetch_ndx100().items():
        merged.setdefault(sym, sector)     # S&P sector wins on overlap
    merged.pop("SPY", None)                # benchmark-only, never a candidate

    if len(merged) < 400:
        print(f"ERROR: only {len(merged)} symbols parsed; upstream page "
              f"likely changed shape. Refusing to write.", file=sys.stderr)
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
```

- [ ] **Step 5: Generate the CSV**

Run: `python scripts/build_universe_sp500_ndx100.py --out config/universe_sp500_ndx100.csv`
Expected: `wrote ~515 symbols to config/universe_sp500_ndx100.csv`

If the script fails because of no network access or a changed upstream page, hand-assemble the CSV instead — the committed file matters, the script does not. Verify with `wc -l config/universe_sp500_ndx100.csv` (expect >400) and `head -3` (expect the header plus two `SYMBOL,Sector` rows).

- [ ] **Step 6: Run tests to verify they pass**

Run: `pytest tests/test_opening_drive_scanner.py -v`
Expected: 5 passed

- [ ] **Step 7: Commit**

```bash
git add strategies/opening_drive_scanner.py scripts/build_universe_sp500_ndx100.py config/universe_sp500_ndx100.csv tests/test_opening_drive_scanner.py
git commit -m "feat(scanner): universe loader with sector map

load_universe returns symbol -> sector (not GapScanner's bare list)
because SectorExposureFilter needs the sector for every candidate.

The committed CSV is the source of truth; the builder script refreshes it
deliberately. Fetching constituents at runtime would make backtests
non-reproducible and change the universe under a running strategy. SPY is
excluded -- it is benchmark-only."
```

---

### Task 3: Baseline model, persistence, and staleness

**Files:**
- Modify: `strategies/opening_drive_scanner.py`
- Test: `tests/test_opening_drive_scanner.py` (append)

**Interfaces:**
- Produces:
  - `OpeningDriveBaseline` frozen dataclass: `atr_14d: float`, `avg_or_volume_20d: float`, `avg_daily_volume_20d: float`, `computed_at: datetime`
  - `load_baselines(path) -> dict[str, OpeningDriveBaseline]`
  - `save_baselines(baselines: dict[str, OpeningDriveBaseline], path) -> None`
  - `baselines_age_p95_days(baselines, now) -> float | None`
  - `baselines_are_stale(baselines, now, max_age_days) -> bool`
  - `baselines_too_old_to_trade(baselines, now, max_age_days) -> bool`

**Context you need:** `GapScanner` learned a lesson the hard way, recorded in its own docstring: using `min(computed_at)` for staleness means one permanently-stale symbol (an IEX name with no daily bars) blocks trading for the *entire* universe. Use the **95th percentile** age instead. Replicate that behaviour here.

`avg_or_volume_20d` is the mean IEX volume in the 09:30–10:00 window over the trailing 20 sessions. It is the denominator of `rvol_or` and is what makes that metric survive the IEX feed.

Note there is no `prev_close` field. The baseline is refreshed at 16:10, so it *could* carry that session's close — but if a refresh is skipped for a day, a stale `prev_close` would corrupt `disp_atr` and `rs_atr` silently. The loop fetches `prev_close` fresh at cut time instead (Task 10).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_opening_drive_scanner.py`:

```python
from datetime import datetime, timedelta, timezone

from strategies.opening_drive_scanner import (
    OpeningDriveBaseline,
    baselines_are_stale,
    baselines_too_old_to_trade,
    load_baselines,
    save_baselines,
)

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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_opening_drive_scanner.py -v`
Expected: FAIL with `ImportError: cannot import name 'OpeningDriveBaseline'`

- [ ] **Step 3: Implement the baseline layer**

Append to `strategies/opening_drive_scanner.py`:

```python
@dataclass(frozen=True)
class OpeningDriveBaseline:
    """Per-symbol trailing statistics, refreshed post-close.

    avg_or_volume_20d is the mean IEX volume in the 09:30-10:00 window over
    the trailing 20 sessions. It is the denominator of rvol_or, and is the
    reason that metric is meaningful on a feed carrying ~2% of consolidated
    volume: the per-symbol IEX share cancels in the ratio.

    avg_daily_volume_20d is likewise IEX-denominated. Do not compare it to
    consolidated-volume thresholds (see the min_avg_daily_volume gate).

    There is deliberately no prev_close field: a skipped refresh would leave
    a stale prev_close that silently corrupts disp_atr and rs_atr. The loop
    fetches prev_close fresh at cut time.
    """
    atr_14d: float
    avg_or_volume_20d: float
    avg_daily_volume_20d: float
    computed_at: datetime


def load_baselines(path: str | Path) -> dict[str, OpeningDriveBaseline]:
    """Load the baselines JSON. A missing file yields {} so the caller can
    refresh; a malformed entry is skipped rather than failing the load."""
    p = Path(path)
    if not p.exists():
        return {}
    try:
        raw = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("OD_BASELINES_LOAD_FAILED path=%s error=%s", p, exc)
        return {}
    out: dict[str, OpeningDriveBaseline] = {}
    for sym, entry in (raw or {}).items():
        try:
            out[sym] = OpeningDriveBaseline(
                atr_14d=float(entry["atr_14d"]),
                avg_or_volume_20d=float(entry["avg_or_volume_20d"]),
                avg_daily_volume_20d=float(entry["avg_daily_volume_20d"]),
                computed_at=datetime.fromisoformat(
                    entry["computed_at"].replace("Z", "+00:00")
                ),
            )
        except (KeyError, TypeError, ValueError) as exc:
            logger.warning("OD_BASELINE_SKIP symbol=%s error=%s", sym, exc)
    return out


def save_baselines(baselines: dict[str, OpeningDriveBaseline],
                   path: str | Path) -> None:
    """Atomic-ish write via tmp + rename, so a crash mid-write cannot leave a
    truncated baselines file that the next session would load as garbage."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        sym: {
            "atr_14d": b.atr_14d,
            "avg_or_volume_20d": b.avg_or_volume_20d,
            "avg_daily_volume_20d": b.avg_daily_volume_20d,
            "computed_at": b.computed_at.astimezone(timezone.utc)
            .isoformat().replace("+00:00", "Z"),
        }
        for sym, b in baselines.items()
    }
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True))
    tmp.replace(p)


def baselines_age_p95_days(
    baselines: dict[str, OpeningDriveBaseline], now: datetime,
) -> float | None:
    """95th-percentile baseline age in days, or None when there are none.

    p95 rather than min(): a handful of permanently stale symbols (IEX names
    with no daily bars) must not block trading for the whole universe. This
    is the same lesson GapScanner records in its own docstring.
    """
    if not baselines:
        return None
    ages = sorted(
        (now - b.computed_at).total_seconds() / 86400.0
        for b in baselines.values()
    )
    return ages[min(int(len(ages) * 0.95), len(ages) - 1)]


def baselines_are_stale(
    baselines: dict[str, OpeningDriveBaseline], now: datetime,
    max_age_days: int,
) -> bool:
    """True when a refresh is recommended."""
    age = baselines_age_p95_days(baselines, now)
    return True if age is None else age > max_age_days


def baselines_too_old_to_trade(
    baselines: dict[str, OpeningDriveBaseline], now: datetime,
    max_age_days: int,
) -> bool:
    """True when baselines are so old that cutting would be unsafe.

    Hard fail-safe at 2x the recommended max age.
    """
    age = baselines_age_p95_days(baselines, now)
    return True if age is None else age > max_age_days * 2
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_opening_drive_scanner.py -v`
Expected: 13 passed

- [ ] **Step 5: Commit**

```bash
git add strategies/opening_drive_scanner.py tests/test_opening_drive_scanner.py
git commit -m "feat(scanner): baseline model, persistence, staleness gates

avg_or_volume_20d is the denominator that makes rvol_or survive an
IEX-only feed -- the per-symbol IEX share cancels in the ratio.

Staleness uses the 95th-percentile age, not min(): GapScanner records the
lesson that one permanently-stale IEX symbol otherwise blocks trading for
the entire universe. Two thresholds: refresh-recommended at max_age_days,
hard refuse-to-cut at 2x.

No prev_close field -- a skipped refresh would leave a stale value that
silently corrupts disp_atr and rs_atr, so the loop fetches it at cut time."
```

---

### Task 4: Opening-range metrics

**Files:**
- Modify: `strategies/opening_drive_scanner.py`
- Test: `tests/test_opening_drive_scanner.py` (append)

**Interfaces:**
- Consumes: `OpeningDriveBaseline` (Task 3), `core.bar.Bar`
- Produces:
  - `OpeningRangeMetrics` frozen dataclass with fields: `symbol: str`, `or_high: float`, `or_low: float`, `or_close: float`, `or_volume: float`, `or_vwap: float`, `prev_close: float`, `atr_14d: float`, `rvol_or: float`, `disp_atr: float`, `or_width_atr: float`, `clv: float`, `rs_atr: float`, `above_vwap: bool`, `bar_coverage: float`
  - `or_return(bars: list[Bar], prev_close: float) -> float | None`
  - `compute_or_metrics(symbol, bars, baseline, prev_close, spy_or_return, or_minutes=30) -> OpeningRangeMetrics | None`

**Context you need:** `Bar` (in `core/bar.py`) is a frozen dataclass with `symbol, ts, open, high, low, close, volume` and a `typical_price` property equal to `(high + low + close) / 3`. Use `typical_price` for the VWAP numerator, matching `setup_orb_vwap.py`.

Return `None` rather than raising for any unusable input. No signal is always preferable to a wrong signal, and a single bad symbol must never abort a 515-symbol cut.

Two division-by-zero cases you must handle explicitly:
- `or_high == or_low` (a flat 30 minutes) → `clv = 0.0`, which then fails the `min_clv` gate naturally.
- `or_volume == 0` → `or_vwap` falls back to `or_close`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_opening_drive_scanner.py`:

```python
from strategies.opening_drive_scanner import (
    OpeningRangeMetrics,
    compute_or_metrics,
    or_return,
)
from core.bar import Bar


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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_opening_drive_scanner.py -v`
Expected: FAIL with `ImportError: cannot import name 'compute_or_metrics'`

- [ ] **Step 3: Implement the metrics**

Append to `strategies/opening_drive_scanner.py`:

```python
@dataclass(frozen=True)
class OpeningRangeMetrics:
    """Self-normalized opening-range statistics for one symbol.

    Every ratio here is either symbol-vs-its-own-history or internal to a
    single feed, so the ~2% IEX volume share cancels. Never add a metric
    that compares one symbol's raw IEX volume to another's.
    """
    symbol: str
    or_high: float
    or_low: float
    or_close: float
    or_volume: float
    or_vwap: float
    prev_close: float
    atr_14d: float
    rvol_or: float
    disp_atr: float
    or_width_atr: float
    clv: float
    rs_atr: float
    above_vwap: bool
    bar_coverage: float


def or_return(bars: list[Bar] | None, prev_close: float) -> float | None:
    """Fractional opening-range return, or None when uncomputable.

    Used for both the candidate and the SPY benchmark leg of rs_atr.
    """
    if not bars or prev_close <= 0:
        return None
    return (bars[-1].close - prev_close) / prev_close


def compute_or_metrics(
    symbol: str,
    bars: list[Bar] | None,
    baseline: OpeningDriveBaseline,
    prev_close: float,
    spy_or_return: float,
    or_minutes: int = 30,
) -> OpeningRangeMetrics | None:
    """Derive all screening metrics for one symbol from its OR bars.

    Returns None for any unusable input rather than raising: no signal is
    preferable to a wrong signal, and one bad symbol must never abort a
    515-symbol cut.
    """
    if not bars or prev_close <= 0 or or_minutes <= 0:
        return None
    if baseline.atr_14d <= 0 or baseline.avg_or_volume_20d <= 0:
        return None

    or_high = max(b.high for b in bars)
    or_low = min(b.low for b in bars)
    or_close = bars[-1].close
    or_volume = sum(b.volume for b in bars)

    # VWAP from typical price, matching setup_orb_vwap.py. A zero-volume
    # window has no VWAP; fall back to the close so above_vwap is False.
    or_vwap = (
        sum(b.typical_price * b.volume for b in bars) / or_volume
        if or_volume > 0 else or_close
    )

    # A flat 30 minutes has no close location; 0.0 fails the min_clv gate.
    rng = or_high - or_low
    clv = ((or_close - or_low) / rng) if rng > 0 else 0.0

    # rs_atr expresses excess return over SPY in units of the symbol's own
    # daily ATR, so a volatile name is not credited for merely being volatile.
    atr_frac = baseline.atr_14d / prev_close
    sym_ret = (or_close - prev_close) / prev_close
    rs_atr = ((sym_ret - spy_or_return) / atr_frac) if atr_frac > 0 else 0.0

    # Denominator is the EXPECTED bar count, not len(bars): a symbol IEX
    # printed in 3 of 30 minutes must score 0.1, not 1.0.
    covered = sum(1 for b in bars if b.volume > 0)

    return OpeningRangeMetrics(
        symbol=symbol,
        or_high=or_high,
        or_low=or_low,
        or_close=or_close,
        or_volume=or_volume,
        or_vwap=or_vwap,
        prev_close=prev_close,
        atr_14d=baseline.atr_14d,
        rvol_or=or_volume / baseline.avg_or_volume_20d,
        disp_atr=(or_close - prev_close) / baseline.atr_14d,
        or_width_atr=rng / baseline.atr_14d,
        clv=clv,
        rs_atr=rs_atr,
        above_vwap=or_close > or_vwap,
        bar_coverage=covered / or_minutes,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_opening_drive_scanner.py -v`
Expected: 26 passed

- [ ] **Step 5: Commit**

```bash
git add strategies/opening_drive_scanner.py tests/test_opening_drive_scanner.py
git commit -m "feat(scanner): self-normalized opening-range metrics

compute_or_metrics derives rvol_or, disp_atr, or_width_atr, clv, rs_atr,
above_vwap and bar_coverage from the 09:30-10:00 bars.

Every metric is a ratio against the symbol's own history or internal to
one feed, so the ~2% IEX volume share cancels. rs_atr expresses excess
return over SPY in the symbol's own ATR units, so a volatile name is not
credited merely for being volatile.

bar_coverage divides by the EXPECTED bar count, not len(bars) -- a symbol
IEX printed in 3 of 30 minutes must score 0.1, not 1.0. Unusable inputs
return None rather than raising, so one bad symbol cannot abort a
515-symbol cut."
```

---

### Task 5: Gates, ranking, and the scanner cut

**Files:**
- Modify: `strategies/opening_drive_scanner.py`
- Test: `tests/test_opening_drive_scanner.py` (append)

**Interfaces:**
- Consumes: `OpeningRangeMetrics`, `OpeningDriveBaseline`, `or_return`, `compute_or_metrics`, `baselines_too_old_to_trade`, `load_universe`
- Produces:
  - `OpeningDriveFilters` frozen dataclass — `min_price=5.0`, `min_avg_daily_volume=100_000.0`, `min_bar_coverage=0.90`, `min_rvol_or=2.0`, `min_disp_atr=0.5`, `min_or_width_atr=0.4`, `max_or_width_atr=2.0`, `min_clv=0.6`, `min_rs_atr=0.0`
  - `ScanResult` frozen dataclass — `symbol: str`, `sector: str`, `metrics: OpeningRangeMetrics`, `score: float`, `cut_ts: datetime`, `side: str = "long"`
  - `gate_reason(m: OpeningRangeMetrics, baseline: OpeningDriveBaseline, f: OpeningDriveFilters) -> str | None` — `None` means all gates pass
  - `rank_score(m: OpeningRangeMetrics) -> float`
  - `OpeningDriveScanner(universe: dict[str, str], baselines: dict[str, OpeningDriveBaseline], filters=OpeningDriveFilters(), max_concurrent_positions: int = 5, candidate_multiplier: float = 1.5, baselines_max_age_days: int = 7, or_minutes: int = 30)`
  - `OpeningDriveScanner.run_cut(bars_by_symbol: dict[str, list[Bar]], prev_closes: dict[str, float], now: datetime) -> list[ScanResult]`

**Context you need:** `run_cut` is pure — it takes already-fetched bars and prev-closes and returns results. All network I/O lives in the loop (Task 10). This is what makes the whole screen testable from fixtures.

`gate_reason` returns the *name of the failing gate* rather than a bool, so rejections are diagnosable in logs. Debugging "why did the scanner return nothing today" without this is miserable.

**The SPY rule is load-bearing.** `rs_atr` needs `spy_or_return`. If SPY's bars are missing, or SPY itself fails `bar_coverage`, `run_cut` MUST return `[]`. Falling back to `spy_or_return=0.0` would silently convert a market-wide rally into five "independent" stock signals — exactly the failure `rs_atr` exists to prevent.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_opening_drive_scanner.py`:

```python
from strategies.opening_drive_scanner import (
    OpeningDriveFilters,
    OpeningDriveScanner,
    ScanResult,
    gate_reason,
    rank_score,
)


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
    """3 one-minute bars ending at `close`, spanning a ~6-point range."""
    return [
        _bar(0, 100.0, 102.0, 99.0, 101.0, volume / 3),
        _bar(1, 101.0, max(105.0, close + 1), 100.5, 103.0, volume / 3),
        _bar(2, 103.0, max(105.0, close + 1), 102.0, close, volume / 3),
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
    """AAA up 0.5% while SPY is up 4% -> negative rs_atr -> gated out."""
    s = _scanner()
    bars = {"AAA": _bars_for(100.5), "SPY": _bars_for(104.0)}
    prev = {"AAA": 100.0, "SPY": 100.0}
    assert s.run_cut(bars, prev, NOW) == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_opening_drive_scanner.py -v`
Expected: FAIL with `ImportError: cannot import name 'OpeningDriveFilters'`

- [ ] **Step 3: Implement gates and ranking**

Append to `strategies/opening_drive_scanner.py`:

```python
@dataclass(frozen=True)
class OpeningDriveFilters:
    """Gate thresholds. These are unvalidated priors, not tuned values --
    starting points for scripts/sweep_equity_strategy.py.

    min_avg_daily_volume is IEX-DENOMINATED. gap_and_go uses 1_000_000,
    which reads as a consolidated-volume figure; against IEX volume (~2% of
    consolidated) that threshold rejects substantially the entire universe
    and the scanner returns nothing, every day, without erroring.
    """
    min_price: float = 5.0
    min_avg_daily_volume: float = 100_000.0
    min_bar_coverage: float = 0.90
    min_rvol_or: float = 2.0
    min_disp_atr: float = 0.5
    min_or_width_atr: float = 0.4
    max_or_width_atr: float = 2.0
    min_clv: float = 0.6
    min_rs_atr: float = 0.0


@dataclass(frozen=True)
class ScanResult:
    symbol: str
    sector: str
    metrics: OpeningRangeMetrics
    score: float
    cut_ts: datetime
    side: str = "long"


def gate_reason(
    m: OpeningRangeMetrics,
    baseline: OpeningDriveBaseline,
    f: OpeningDriveFilters,
) -> str | None:
    """Return the name of the first failing gate, or None if all pass.

    Returns the gate NAME rather than a bool so rejections are diagnosable:
    answering "why did the scanner return nothing today" from logs is
    otherwise guesswork.
    """
    if m.or_close < f.min_price:
        return "min_price"
    if baseline.avg_daily_volume_20d < f.min_avg_daily_volume:
        return "min_avg_daily_volume"
    if m.bar_coverage < f.min_bar_coverage:
        return "min_bar_coverage"
    if m.rvol_or < f.min_rvol_or:
        return "min_rvol_or"
    if m.disp_atr < f.min_disp_atr:
        return "min_disp_atr"
    if m.or_width_atr < f.min_or_width_atr:
        return "min_or_width_atr"
    if m.or_width_atr > f.max_or_width_atr:
        return "max_or_width_atr"
    if m.clv < f.min_clv:
        return "min_clv"
    if not m.above_vwap:
        return "above_vwap"
    if m.rs_atr <= f.min_rs_atr:
        return "min_rs_atr"
    return None


def rank_score(m: OpeningRangeMetrics) -> float:
    """Two-factor rank: participation x idiosyncratic strength.

    Deliberately mirrors GapScanner's gap_atr_mult * rvol shape so the
    existing sweep harness applies unchanged and no new overfitting surface
    is introduced. Everything else is a gate, not a rank term.
    """
    return m.rvol_or * m.rs_atr


class OpeningDriveScanner:
    """Holds the universe, baselines, and gate configuration.

    run_cut is PURE: it takes already-fetched bars and prev-closes and
    returns results. All network I/O lives in OpeningDriveLoop, which is
    what makes the entire screen testable from fixtures.
    """

    BENCHMARK = "SPY"

    def __init__(
        self,
        universe: dict[str, str],
        baselines: dict[str, OpeningDriveBaseline],
        filters: OpeningDriveFilters = OpeningDriveFilters(),
        max_concurrent_positions: int = 5,
        candidate_multiplier: float = 1.5,
        baselines_max_age_days: int = 7,
        or_minutes: int = 30,
    ) -> None:
        if not universe:
            raise ValueError("OpeningDriveScanner universe is empty")
        self.universe = dict(universe)
        self.baselines = dict(baselines)
        self.filters = filters
        self.max_concurrent_positions = max_concurrent_positions
        self.candidate_multiplier = candidate_multiplier
        self.baselines_max_age_days = baselines_max_age_days
        self.or_minutes = or_minutes

    def request_symbols(self) -> list[str]:
        """Universe plus the benchmark. SPY is not an index constituent, so
        it must be appended explicitly or rs_atr is uncomputable."""
        return sorted(self.universe) + [self.BENCHMARK]

    def _spy_or_return(
        self, bars_by_symbol: dict[str, list[Bar]],
        prev_closes: dict[str, float],
    ) -> float | None:
        """Benchmark return, or None if the benchmark is unusable.

        None must propagate to an empty watchlist. Substituting 0.0 would
        turn a market-wide rally into five 'independent' stock signals --
        precisely the failure rs_atr exists to prevent.
        """
        spy_bars = bars_by_symbol.get(self.BENCHMARK)
        spy_prev = prev_closes.get(self.BENCHMARK, 0.0)
        if not spy_bars or spy_prev <= 0:
            return None
        covered = sum(1 for b in spy_bars if b.volume > 0)
        if covered / self.or_minutes < self.filters.min_bar_coverage:
            return None
        return or_return(spy_bars, spy_prev)

    def run_cut(
        self,
        bars_by_symbol: dict[str, list[Bar]],
        prev_closes: dict[str, float],
        now: datetime,
    ) -> list[ScanResult]:
        """Apply gates and ranking; return the day's ranked watchlist."""
        if baselines_too_old_to_trade(
            self.baselines, now, self.baselines_max_age_days,
        ):
            logger.error(
                "OD_BASELINES_TOO_STALE p95_age_days=%.1f max=%d "
                "— refusing to cut",
                baselines_age_p95_days(self.baselines, now) or float("inf"),
                self.baselines_max_age_days * 2,
            )
            return []

        spy_ret = self._spy_or_return(bars_by_symbol, prev_closes)
        if spy_ret is None:
            logger.error(
                "OD_BENCHMARK_UNAVAILABLE symbol=%s — refusing to cut "
                "(an unbenchmarked ranking would mistake a market-wide move "
                "for independent stock signals)", self.BENCHMARK,
            )
            return []

        candidates: list[ScanResult] = []
        rejects: dict[str, int] = {}
        for symbol, sector in self.universe.items():
            baseline = self.baselines.get(symbol)
            if baseline is None:
                rejects["no_baseline"] = rejects.get("no_baseline", 0) + 1
                continue
            m = compute_or_metrics(
                symbol,
                bars_by_symbol.get(symbol),
                baseline,
                prev_closes.get(symbol, 0.0),
                spy_ret,
                or_minutes=self.or_minutes,
            )
            if m is None:
                rejects["no_metrics"] = rejects.get("no_metrics", 0) + 1
                continue
            reason = gate_reason(m, baseline, self.filters)
            if reason is not None:
                rejects[reason] = rejects.get(reason, 0) + 1
                continue
            candidates.append(ScanResult(
                symbol=symbol, sector=sector, metrics=m,
                score=rank_score(m), cut_ts=now, side="long",
            ))

        candidates.sort(key=lambda c: c.score, reverse=True)
        top_n = max(1, math.ceil(
            self.max_concurrent_positions * self.candidate_multiplier,
        ))
        kept = candidates[:top_n]
        logger.info(
            "OD_CUT_DONE qualifiers=%d kept=%d spy_or_return=%.4f rejects=%s",
            len(candidates), len(kept), spy_ret,
            dict(sorted(rejects.items(), key=lambda kv: -kv[1])),
        )
        return kept
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_opening_drive_scanner.py -v`
Expected: 47 passed

- [ ] **Step 5: Commit**

```bash
git add strategies/opening_drive_scanner.py tests/test_opening_drive_scanner.py
git commit -m "feat(scanner): gate cascade, two-factor ranking, run_cut

gate_reason returns the failing gate's NAME rather than a bool, and
run_cut logs a reject histogram -- answering 'why did the scanner return
nothing today' is otherwise guesswork.

Ranking is rvol_or * rs_atr, mirroring GapScanner's two-factor shape so
the existing sweep harness applies and no new overfitting surface appears.

SPY handling is load-bearing: a missing, stale, or sparsely-printed
benchmark returns an empty watchlist rather than falling back to
spy_or_return=0.0, which would turn a market-wide rally into five
'independent' stock signals.

The ADV gate defaults to 100_000 and is IEX-denominated, with a regression
test pinning it -- gap_and_go's 1_000_000 is a consolidated figure that
would reject the whole universe silently."
```

---

### Task 6: SectorExposureFilter

**Files:**
- Modify: `risk/filters.py` (add after `ConcurrentPositionFilter`, which ends at line 165)
- Test: `tests/test_sector_exposure_filter.py`

**Interfaces:**
- Consumes: `EntryFilter`, `FilterResult` (existing, `risk/filters.py`); `PositionBook.all()` returning `list[OpenPosition]` where each has `.symbol` and `.setup`
- Produces: `SectorExposureFilter(sector_map: dict[str, str], max_per_sector: int = 2, setup_name: str | None = None)`

**Context you need:** `EntryFilter.check(self, signal, ctx, ledger, book) -> FilterResult` is the interface; `FilterResult.ok()` and `FilterResult.reject(reason)` are the constructors. `FilterPipeline` prefixes the filter's `name` onto the reason, so the reason text should not repeat it.

`PositionBook` holds positions for potentially several setups. `setup_name` scopes counting to this strategy's own positions — without it, an unrelated strategy's position in the same sector would consume this strategy's sector budget. When `setup_name` is `None`, count everything (useful in tests).

Symbols absent from `sector_map` count as `"UNKNOWN"`, matching `load_universe`. `UNKNOWN` is its own bucket and is capped like any other — safer than exempting it.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_sector_exposure_filter.py
from datetime import datetime, timezone

from risk.filters import SectorExposureFilter
from state.position_book import OpenPosition, PositionBook
from strategies.base_setup import SetupSignal

TS = datetime(2026, 8, 28, 14, 5, tzinfo=timezone.utc)
SECTORS = {"AAA": "Tech", "BBB": "Tech", "CCC": "Tech", "XXX": "Energy"}


def _signal(symbol: str) -> SetupSignal:
    return SetupSignal(
        setup="opening_drive", symbol=symbol, side="long",
        entry=100.0, stop=98.0, target=104.0, atr=2.0, level=100.0, ts=TS,
    )


def _book(*symbols: str, setup: str = "opening_drive") -> PositionBook:
    b = PositionBook()
    for s in symbols:
        b.add(OpenPosition(
            symbol=s, setup=setup, side="long", qty=10, entry_px=100.0,
            stop_px=98.0, target_px=104.0, opened_at=TS, order_id=f"o-{s}",
        ))
    return b


def test_allows_first_position_in_sector():
    f = SectorExposureFilter(SECTORS, max_per_sector=2)
    assert f.check(_signal("AAA"), None, None, _book()).passed


def test_allows_second_position_in_sector():
    f = SectorExposureFilter(SECTORS, max_per_sector=2)
    assert f.check(_signal("BBB"), None, None, _book("AAA")).passed


def test_rejects_third_position_in_same_sector():
    f = SectorExposureFilter(SECTORS, max_per_sector=2)
    res = f.check(_signal("CCC"), None, None, _book("AAA", "BBB"))
    assert not res.passed
    assert "Tech" in res.reason


def test_other_sector_unaffected_by_a_full_sector():
    f = SectorExposureFilter(SECTORS, max_per_sector=2)
    assert f.check(_signal("XXX"), None, None, _book("AAA", "BBB")).passed


def test_none_book_passes():
    f = SectorExposureFilter(SECTORS, max_per_sector=2)
    assert f.check(_signal("AAA"), None, None, None).passed


def test_unknown_symbols_share_the_unknown_bucket():
    f = SectorExposureFilter({}, max_per_sector=1)
    res = f.check(_signal("AAA"), None, None, _book("ZZZ"))
    assert not res.passed
    assert "UNKNOWN" in res.reason


def test_other_setups_positions_do_not_consume_our_budget():
    """Scoping matters: another strategy's Tech position must not eat this
    strategy's sector budget."""
    f = SectorExposureFilter(SECTORS, max_per_sector=2,
                             setup_name="opening_drive")
    book = _book("AAA", setup="opening_drive")
    for p in _book("BBB", setup="vwap_wave").all():
        book.add(p)
    assert f.check(_signal("CCC"), None, None, book).passed


def test_unscoped_filter_counts_every_setup():
    f = SectorExposureFilter(SECTORS, max_per_sector=2, setup_name=None)
    book = _book("AAA", setup="opening_drive")
    for p in _book("BBB", setup="vwap_wave").all():
        book.add(p)
    assert not f.check(_signal("CCC"), None, None, book).passed


def test_zero_cap_rejects_everything():
    f = SectorExposureFilter(SECTORS, max_per_sector=0)
    assert not f.check(_signal("AAA"), None, None, _book()).passed


def test_filter_name_is_stable():
    assert SectorExposureFilter(SECTORS).name == "sector_exposure"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_sector_exposure_filter.py -v`
Expected: FAIL with `ImportError: cannot import name 'SectorExposureFilter'`

- [ ] **Step 3: Implement the filter**

Add to `risk/filters.py`, directly after `ConcurrentPositionFilter`:

```python
class SectorExposureFilter(EntryFilter):
    """Cap concurrent positions per sector.

    Without this, "top-N at full risk each" silently becomes one leveraged
    sector bet: five semiconductor longs are one risk, not five. This is the
    filter that makes the portfolio-level risk claim actually true.

    ``setup_name`` scopes counting to this strategy's own positions. The
    PositionBook may hold several setups' positions, and an unrelated
    strategy's holding in the same sector must not consume this strategy's
    sector budget. Pass None to count every setup.

    Symbols missing from ``sector_map`` fall into an "UNKNOWN" bucket, which
    is capped like any other sector -- exempting unknowns would let a stale
    universe file quietly bypass the cap.
    """
    name = "sector_exposure"

    def __init__(self, sector_map: dict[str, str], max_per_sector: int = 2,
                 setup_name: str | None = None):
        self.sector_map = dict(sector_map)
        self.max_per_sector = max_per_sector
        self.setup_name = setup_name

    def _sector_of(self, symbol: str) -> str:
        return self.sector_map.get(symbol.upper(), "UNKNOWN")

    def check(self, signal, ctx, ledger, book: PositionBook | None) -> FilterResult:
        if book is None:
            return FilterResult.ok()
        target = self._sector_of(signal.symbol)
        held = 0
        for pos in book.all():
            if self.setup_name is not None and pos.setup != self.setup_name:
                continue
            if self._sector_of(pos.symbol) == target:
                held += 1
        if held >= self.max_per_sector:
            return FilterResult.reject(
                f"sector {target} already has {held} open "
                f"(max {self.max_per_sector})"
            )
        return FilterResult.ok()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_sector_exposure_filter.py -v`
Expected: 10 passed

- [ ] **Step 5: Run the full suite**

Run: `pytest -q`
Expected: no new failures

- [ ] **Step 6: Commit**

```bash
git add risk/filters.py tests/test_sector_exposure_filter.py
git commit -m "feat(risk): SectorExposureFilter caps concurrent positions per sector

Without a sector cap, 'top-N at full risk each' silently becomes one
leveraged sector bet -- five semiconductor longs are one risk, not five.

Counting is scoped by setup_name so another strategy's position in the
same sector cannot consume this strategy's sector budget. Symbols missing
from the sector map share an UNKNOWN bucket that is capped like any other,
so a stale universe file cannot quietly bypass the cap."
```

---

### Task 7: PDT guard

**Files:**
- Create: `risk/pdt_guard.py`
- Test: `tests/test_pdt_guard.py`

**Interfaces:**
- Produces:
  - `PDTViolation(Exception)`
  - `check_pdt_headroom(account: dict, *, min_equity: float = 25_000.0, enabled: bool = True) -> None` — raises `PDTViolation`, else returns

**Context you need:** `AlpacaClient.get_account()` returns the raw Alpaca account dict. Relevant keys: `equity` (string), `pattern_day_trader` (bool), `account_blocked`/`trading_blocked` (bool). Paper accounts report `"paper"` nowhere useful — the reliable discriminator is the base URL, so the caller passes `enabled` from config rather than sniffing the account.

FINRA's rule: a margin account under $25,000 equity is limited to 3 day trades per rolling 5 business days. This strategy generates 4+ per session, so it would trip on day one. Paper accounts do not enforce PDT, which is exactly why the guard must be **tested** rather than assumed — the failure only appears in live trading, where it costs a 90-day closing-only restriction.

The guard is a **boot precondition**, not an entry filter. Failing fast at startup is far better than discovering it mid-session with positions open.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_pdt_guard.py
import pytest

from risk.pdt_guard import PDTViolation, check_pdt_headroom


def _account(**kw) -> dict:
    base = {
        "equity": "100000",
        "pattern_day_trader": False,
        "account_blocked": False,
        "trading_blocked": False,
    }
    base.update(kw)
    return base


def test_ample_equity_passes():
    check_pdt_headroom(_account(equity="100000"))


def test_equity_exactly_at_threshold_passes():
    check_pdt_headroom(_account(equity="25000"))


def test_equity_below_threshold_raises():
    with pytest.raises(PDTViolation, match="25000"):
        check_pdt_headroom(_account(equity="24999"))


def test_disabled_guard_permits_low_equity():
    """Paper accounts do not enforce PDT; the guard is config-gated."""
    check_pdt_headroom(_account(equity="1000"), enabled=False)


def test_flagged_pattern_day_trader_below_threshold_raises():
    with pytest.raises(PDTViolation):
        check_pdt_headroom(_account(equity="20000", pattern_day_trader=True))


def test_flagged_pattern_day_trader_above_threshold_passes():
    check_pdt_headroom(_account(equity="30000", pattern_day_trader=True))


def test_blocked_account_raises_even_with_ample_equity():
    with pytest.raises(PDTViolation, match="blocked"):
        check_pdt_headroom(_account(trading_blocked=True))
    with pytest.raises(PDTViolation, match="blocked"):
        check_pdt_headroom(_account(account_blocked=True))


def test_custom_threshold_respected():
    with pytest.raises(PDTViolation):
        check_pdt_headroom(_account(equity="40000"), min_equity=50_000.0)


def test_missing_equity_key_raises_rather_than_assuming_safe():
    with pytest.raises(PDTViolation, match="equity"):
        check_pdt_headroom({"pattern_day_trader": False})


def test_unparseable_equity_raises():
    with pytest.raises(PDTViolation, match="equity"):
        check_pdt_headroom(_account(equity="not-a-number"))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_pdt_guard.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'risk.pdt_guard'`

- [ ] **Step 3: Implement the guard**

Create `risk/pdt_guard.py`:

```python
"""Pattern Day Trader precondition check.

FINRA limits a US margin account under $25,000 equity to 3 day trades per
rolling 5 business days; a breach restricts the account to closing-only for
90 days. Opening Drive opens and closes every position intraday, generating
4+ day trades per session, so it would trip the rule on its first day.

Paper accounts do not enforce PDT. That is precisely why this guard is
tested rather than assumed: the failure surfaces only in live trading, and
the cost there is a 90-day restriction, not an error message.

This is a BOOT PRECONDITION, not an entry filter. Refusing to start is far
better than discovering the problem mid-session with positions open.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


class PDTViolation(Exception):
    """Raised when the account cannot safely day-trade this strategy."""


def check_pdt_headroom(
    account: dict,
    *,
    min_equity: float = 25_000.0,
    enabled: bool = True,
) -> None:
    """Raise PDTViolation when the account lacks day-trading headroom.

    ``enabled`` is passed from config rather than sniffed from the account
    dict: Alpaca does not expose a reliable paper/live discriminator in the
    account payload, so the caller (which knows its base URL) decides.
    """
    if not enabled:
        logger.info("PDT_GUARD_DISABLED — skipping headroom check")
        return

    if account.get("trading_blocked") or account.get("account_blocked"):
        raise PDTViolation(
            "account is blocked for trading — refusing to start"
        )

    raw = account.get("equity")
    if raw is None:
        raise PDTViolation(
            "account payload has no 'equity' field — refusing to start "
            "rather than assume PDT headroom exists"
        )
    try:
        equity = float(raw)
    except (TypeError, ValueError):
        raise PDTViolation(
            f"could not parse account 'equity' value {raw!r} — refusing to "
            f"start rather than assume PDT headroom exists"
        ) from None

    if equity < min_equity:
        raise PDTViolation(
            f"account equity {equity:.2f} is below the PDT threshold "
            f"{min_equity:.0f}. This strategy generates 4+ day trades per "
            f"session, which would trigger a 90-day closing-only "
            f"restriction. Set risk.pdt_guard_enabled: false only for a "
            f"paper account."
        )

    logger.info(
        "PDT_GUARD_OK equity=%.2f threshold=%.0f flagged_pdt=%s",
        equity, min_equity, bool(account.get("pattern_day_trader")),
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_pdt_guard.py -v`
Expected: 11 passed

- [ ] **Step 5: Commit**

```bash
git add risk/pdt_guard.py tests/test_pdt_guard.py
git commit -m "feat(risk): PDT headroom boot precondition

A margin account under \$25k is limited to 3 day trades per rolling 5
business days. This strategy generates 4+ per session, so it would trip
the rule on day one and earn a 90-day closing-only restriction.

Paper accounts do not enforce PDT, which is exactly why the guard is
tested rather than assumed -- the failure only appears live. Missing or
unparseable equity raises rather than defaulting to safe.

Boot precondition, not an entry filter: refusing to start beats
discovering the problem mid-session with positions open."
```

---

### Task 8: OpeningDriveSetup trigger state machine

**Files:**
- Create: `strategies/setup_opening_drive.py`
- Test: `tests/test_opening_drive_setup.py`

**Interfaces:**
- Consumes: `BaseSetup`, `SetupSignal` (`strategies/base_setup.py`); `SessionContext` (`core/session.py`) — uses `ctx.bars` and the `ctx.vwap` property
- Produces:
  - `OpeningDriveSetup(symbol, or_high, or_low, atr_14d, avg_minute_volume, entry_deadline, volume_confirm_mult=2.0, target_R=2.0, min_stop_atr_frac=0.15, atr_mult_stop_cap=2.0)` with `name = "opening_drive"`
  - `.check(ctx) -> SetupSignal | None`, `.reset() -> None`

**Context you need:**

`BaseSetup.__init__(symbol)` sets `self.state = "IDLE"`. This setup starts life already armed (it is only constructed for symbols that passed the cut), so `__init__` sets `self.state = "ARMED"` after calling `super().__init__`.

**No separate pullback state is needed, and this is worth understanding.** `or_high` is the high of 09:30–10:00, so `or_close <= or_high` holds by definition, and the `min_clv` gate guarantees the close sits in the upper 40% of the range. The candidate therefore always begins below its trigger level. The state machine is simply: wait for a post-10:00 bar that closes above `or_high` with volume confirmation and above VWAP.

`SetupSignal` is frozen with required fields `setup, symbol, side, entry, stop, target, atr, level, ts` and an optional `notes: dict`.

**Stop placement — two guards the spec left to implementation:**
- **Floor.** If the structural risk (`entry - running_low`) is smaller than `min_stop_atr_frac * atr_14d`, widen it to that floor. A trigger bar that is its own low would otherwise produce a near-zero stop distance and an absurd share count.
- **Ceiling.** If the structural risk exceeds `atr_mult_stop_cap * atr_14d`, **reject the trigger** rather than clamping. Clamping tighter than structure defeats the purpose of a structural stop; a retracement that deep simply means the R:R is not there.

`avg_minute_volume` is `or_volume / or_minutes` from the cut — a stable, already-available reference. Volume confirmation is `bar.volume >= volume_confirm_mult * avg_minute_volume`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_opening_drive_setup.py
from datetime import datetime, timedelta, timezone

import pytest

from core.asset_class import AssetClassConfig
from core.bar import Bar
from core.session import SessionContext
from strategies.setup_opening_drive import OpeningDriveSetup

OR_HIGH = 105.0
OR_LOW = 99.0
ATR = 4.0
AVG_MIN_VOL = 1_000.0
CUT = datetime(2026, 8, 28, 14, 0, tzinfo=timezone.utc)      # 10:00 NY
DEADLINE = datetime(2026, 8, 28, 15, 0, tzinfo=timezone.utc)  # 11:00 NY

EQUITY_AC = AssetClassConfig(
    name="equity", timezone="America/New_York",
    session_open_local="09:30", session_close_local="16:00",
    opening_blackout_min=0, bar_timeframe="1Min",
    slippage_bps=2.0, commission_per_share=0.0, commission_bps=0.0,
)


def _bar(minute: int, o, h, l, c, v) -> Bar:
    return Bar(symbol="TEST", ts=CUT + timedelta(minutes=minute),
               open=o, high=h, low=l, close=c, volume=v)


def _setup(**kw) -> OpeningDriveSetup:
    params = dict(
        symbol="TEST", or_high=OR_HIGH, or_low=OR_LOW, atr_14d=ATR,
        avg_minute_volume=AVG_MIN_VOL, entry_deadline=DEADLINE,
        volume_confirm_mult=2.0, target_R=2.0,
        min_stop_atr_frac=0.15, atr_mult_stop_cap=2.0,
    )
    params.update(kw)
    return OpeningDriveSetup(**params)


def _ctx(bars: list[Bar]) -> SessionContext:
    ctx = SessionContext(symbol="TEST", asset_class=EQUITY_AC)
    for b in bars:
        ctx.ingest(b)
    return ctx


def _feed(setup: OpeningDriveSetup, bars: list[Bar]):
    """Ingest bars one at a time, returning the first signal produced."""
    ctx = SessionContext(symbol="TEST", asset_class=EQUITY_AC)
    for b in bars:
        ctx.ingest(b)
        sig = setup.check(ctx)
        if sig is not None:
            return sig
    return None


# Low-VWAP seed bar so ctx.vwap stays well under the trigger price.
SEED = _bar(0, 100.0, 100.5, 99.0, 100.0, 1_000)
# Reclaim bar: closes above OR_HIGH, volume 3x the minute average.
RECLAIM = _bar(1, 104.0, 106.0, 103.0, 105.5, 3_000)


def test_starts_armed():
    assert _setup().state == "ARMED"


def test_no_signal_while_close_below_or_high():
    s = _setup()
    assert _feed(s, [SEED, _bar(1, 104.0, 104.9, 103.0, 104.5, 5_000)]) is None
    assert s.state == "ARMED"


def test_fires_on_reclaim_with_volume_and_above_vwap():
    s = _setup()
    sig = _feed(s, [SEED, RECLAIM])
    assert sig is not None
    assert sig.setup == "opening_drive"
    assert sig.symbol == "TEST"
    assert sig.side == "long"
    assert sig.entry == 105.5
    assert sig.level == OR_HIGH
    assert sig.atr == ATR
    assert s.state == "FILLED"


def test_no_signal_without_volume_confirmation():
    s = _setup()
    weak = _bar(1, 104.0, 106.0, 103.0, 105.5, 1_999)   # < 2x 1000
    assert _feed(s, [SEED, weak]) is None
    assert s.state == "ARMED"


def test_volume_confirmation_boundary_is_inclusive():
    s = _setup()
    exact = _bar(1, 104.0, 106.0, 103.0, 105.5, 2_000)
    assert _feed(s, [SEED, exact]) is not None


def test_no_signal_when_close_at_or_below_vwap():
    """A high-priced, high-volume seed pushes VWAP above the reclaim close."""
    s = _setup()
    rich_seed = _bar(0, 120.0, 121.0, 119.0, 120.0, 500_000)
    assert _feed(s, [rich_seed, RECLAIM]) is None
    assert s.state == "ARMED"


def test_stop_sits_at_the_running_low():
    s = _setup()
    dip = _bar(1, 104.0, 104.5, 101.0, 104.0, 1_000)     # low 101, no trigger
    reclaim = _bar(2, 104.0, 106.0, 103.5, 105.5, 3_000)
    sig = _feed(s, [SEED, dip, reclaim])
    assert sig is not None
    assert sig.stop == pytest.approx(99.0)   # SEED's low is the running low
    assert sig.entry - sig.stop == pytest.approx(6.5)


def test_stop_floored_at_min_stop_atr_frac():
    """A trigger bar that is its own low would give a near-zero stop."""
    s = _setup(min_stop_atr_frac=0.5)        # floor = 0.5 * 4.0 = 2.0
    tight = _bar(0, 105.4, 105.6, 105.4, 105.5, 3_000)
    sig = _feed(s, [tight])
    assert sig is not None
    assert sig.stop == pytest.approx(105.5 - 2.0)


def test_rejects_trigger_when_structural_stop_exceeds_cap():
    """Risk beyond atr_mult_stop_cap * ATR is rejected, never clamped."""
    s = _setup(atr_mult_stop_cap=1.0)        # cap = 4.0
    deep = _bar(0, 100.0, 100.5, 95.0, 100.0, 1_000)     # low 95
    reclaim = _bar(1, 104.0, 106.0, 103.0, 105.5, 3_000)  # risk 10.5 > 4.0
    assert _feed(s, [deep, reclaim]) is None
    assert s.state == "ARMED"


def test_target_is_target_R_multiples_of_risk():
    s = _setup(target_R=3.0)
    sig = _feed(s, [SEED, RECLAIM])
    risk = sig.entry - sig.stop
    assert sig.target == pytest.approx(sig.entry + 3.0 * risk)


def test_expires_at_entry_deadline():
    s = _setup()
    late = Bar(symbol="TEST", ts=DEADLINE, open=104.0, high=106.0,
               low=103.0, close=105.5, volume=9_000)
    assert _feed(s, [SEED, late]) is None
    assert s.state == "EXPIRED"


def test_expired_setup_ignores_later_bars():
    s = _setup()
    late = Bar(symbol="TEST", ts=DEADLINE, open=104.0, high=106.0,
               low=103.0, close=105.5, volume=9_000)
    _feed(s, [SEED, late])
    assert s.state == "EXPIRED"
    assert s.check(_ctx([SEED, RECLAIM])) is None


def test_fires_only_once():
    s = _setup()
    ctx = SessionContext(symbol="TEST", asset_class=EQUITY_AC)
    for b in [SEED, RECLAIM]:
        ctx.ingest(b)
        s.check(ctx)
    again = _bar(2, 105.5, 107.0, 105.0, 106.5, 9_000)
    ctx.ingest(again)
    assert s.check(ctx) is None


def test_empty_context_is_safe():
    s = _setup()
    assert s.check(SessionContext(symbol="TEST", asset_class=EQUITY_AC)) is None


def test_notes_carry_trigger_diagnostics():
    s = _setup()
    sig = _feed(s, [SEED, RECLAIM])
    assert sig.notes["style"] == "or_high_reclaim"
    assert sig.notes["or_high"] == OR_HIGH
    assert sig.notes["or_low"] == OR_LOW
    assert sig.notes["structural_low"] == pytest.approx(99.0)
    assert sig.notes["stop_floored"] is False


def test_reset_returns_to_armed():
    s = _setup()
    _feed(s, [SEED, RECLAIM])
    assert s.state == "FILLED"
    s.reset()
    assert s.state == "ARMED"
    assert _feed(s, [SEED, RECLAIM]) is not None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_opening_drive_setup.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'strategies.setup_opening_drive'`

- [ ] **Step 3: Implement the setup**

Create `strategies/setup_opening_drive.py`:

```python
"""Opening Drive entry trigger: OR-high reclaim with volume confirmation.

Constructed only for symbols that passed the 10:00 scanner cut, so the setup
starts ARMED rather than IDLE.

No separate pullback state is required. or_high is the high of the 09:30-10:00
window, so or_close <= or_high holds by definition, and the min_clv gate
guarantees the close sits in the upper part of the range. Every candidate
therefore begins below its own trigger level; the machine only has to wait for
a post-cut bar to close above it.

States: ARMED -> FILLED (signal emitted) or ARMED -> EXPIRED (deadline passed).
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from core.session import SessionContext
from strategies.base_setup import BaseSetup, SetupSignal

logger = logging.getLogger(__name__)


class OpeningDriveSetup(BaseSetup):
    name = "opening_drive"

    def __init__(
        self,
        symbol: str,
        or_high: float,
        or_low: float,
        atr_14d: float,
        avg_minute_volume: float,
        entry_deadline: datetime,
        volume_confirm_mult: float = 2.0,
        target_R: float = 2.0,
        min_stop_atr_frac: float = 0.15,
        atr_mult_stop_cap: float = 2.0,
    ) -> None:
        super().__init__(symbol)
        self.or_high = or_high
        self.or_low = or_low
        self.atr_14d = atr_14d
        self.avg_minute_volume = avg_minute_volume
        self.entry_deadline = entry_deadline
        self.volume_confirm_mult = volume_confirm_mult
        self.target_R = target_R
        self.min_stop_atr_frac = min_stop_atr_frac
        self.atr_mult_stop_cap = atr_mult_stop_cap
        self.state = "ARMED"
        self._run_low: float = float("inf")

    def reset(self) -> None:
        super().reset()
        self.state = "ARMED"
        self._run_low = float("inf")

    def check(self, ctx: SessionContext) -> Optional[SetupSignal]:
        if not ctx.bars:
            return None
        if self.state != "ARMED":
            return None

        bar = ctx.bars[-1]

        if bar.ts >= self.entry_deadline:
            self.state = "EXPIRED"
            logger.info("OD_SETUP_EXPIRED symbol=%s deadline=%s",
                        self.symbol, self.entry_deadline.isoformat())
            return None

        # Track the retracement low across the whole entry window, including
        # the trigger bar itself — this becomes the structural stop.
        self._run_low = min(self._run_low, bar.low)

        if bar.close <= self.or_high:
            return None
        if bar.volume < self.volume_confirm_mult * self.avg_minute_volume:
            return None
        if bar.close <= ctx.vwap:
            return None

        entry = bar.close
        structural_low = self._run_low
        risk = entry - structural_low

        max_risk = self.atr_mult_stop_cap * self.atr_14d
        if risk > max_risk:
            # Reject rather than clamp: clamping tighter than structure
            # defeats the point of a structural stop, and a retracement this
            # deep simply means the R:R is not there.
            logger.info(
                "OD_TRIGGER_REJECTED_WIDE_STOP symbol=%s risk=%.4f cap=%.4f",
                self.symbol, risk, max_risk,
            )
            return None

        min_risk = self.min_stop_atr_frac * self.atr_14d
        stop_floored = risk < min_risk
        if stop_floored:
            risk = min_risk

        stop = entry - risk
        target = entry + self.target_R * risk
        self.state = "FILLED"

        return SetupSignal(
            setup=self.name, symbol=self.symbol, side="long",
            entry=entry, stop=stop, target=target,
            atr=self.atr_14d, level=self.or_high, ts=bar.ts,
            notes={
                "style": "or_high_reclaim",
                "or_high": self.or_high,
                "or_low": self.or_low,
                "structural_low": structural_low,
                "stop_floored": stop_floored,
                "trigger_volume": bar.volume,
                "avg_minute_volume": self.avg_minute_volume,
            },
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_opening_drive_setup.py -v`
Expected: 17 passed

- [ ] **Step 5: Commit**

```bash
git add strategies/setup_opening_drive.py tests/test_opening_drive_setup.py
git commit -m "feat(strategies): OpeningDriveSetup OR-high reclaim trigger

Fires when a post-cut bar closes above the opening-range high with volume
>= volume_confirm_mult x the OR's average minute volume, and above session
VWAP. Starts ARMED (constructed only for symbols that passed the cut).

No pullback state is needed: or_close <= or_high by definition and the
min_clv gate keeps the close in the upper range, so every candidate starts
below its trigger level.

Stops sit at the running retracement low, floored at min_stop_atr_frac x
ATR so a trigger bar that is its own low cannot produce an absurd share
count. Risk beyond atr_mult_stop_cap x ATR is REJECTED, not clamped --
clamping tighter than structure defeats a structural stop."
```

---

### Task 9: OpeningDriveLoop daily orchestrator

**Files:**
- Create: `scheduler/opening_drive_loop.py`
- Test: `tests/test_opening_drive_loop.py`

**Interfaces:**
- Consumes: everything from Tasks 1, 3, 4, 5, 8; `AssetClassConfig`, `SessionContext`, `PositionBook`, `OrderExecutor.submit/close_position/handle_actions`, `RiskManager.evaluate`, `AlpacaClient.cancel_order`
- Produces:
  - `OpeningDriveConfig` dataclass — `universe_path, baselines_path, baselines_max_age_days=7, or_minutes=30, entry_window_minutes=60, volume_confirm_mult=2.0, target_R=2.0, min_stop_atr_frac=0.15, atr_mult_stop_cap=2.0, max_concurrent_positions=5, candidate_multiplier=1.5, premarket_bar_timeframe="1Min", regular_bar_timeframe="5Min"`
  - `OpeningDriveLoop(cfg, scanner, equity_asset_class, alpaca_client, alpaca_data, risk_manager, executor, book, position_manager, strategy_name)`
  - Phase methods: `or_window(day) -> tuple[datetime, datetime]`, `cut_time(day)`, `entry_window_end(day)`, `eod_close_time(day)`, `fetch_prev_closes(symbols, now)`, `run_cut(now)`, `on_bar(symbol, bar)`, `manage_open(symbol, bar)`, `force_close_all(now)`, `reset_for_new_day()`

**Context you need:**

**Seed the SessionContext with the OR bars.** This is the subtlety most likely to be got wrong. `OpeningDriveSetup` compares `bar.close` against `ctx.vwap`, and `ctx.vwap` is computed only from the bars the context has ingested. If the context is created empty at the cut, `ctx.vwap` is the VWAP of post-10:00 bars only — not session VWAP from 09:30 — and the VWAP filter becomes almost meaningless in the first minutes after the cut. Ingest the 09:30–10:00 bars into each context at cut time.

**`prev_close` must exclude today's partial daily bar.** A `1Day` request whose window touches the current session can return a partial bar for today. Filter to bars whose NY date is strictly before today's, then take the last.

**`force_close_all` must cancel OCO children first.** `OrderExecutor.close_position` submits a market close but does **not** cancel the bracket legs. Flattening while the stop and target remain live leaves orphaned orders. Cancel `pos.stop_order_id` and `pos.target_order_id` via `self.alpaca.cancel_order`, tolerating failures (an already-filled leg raises), then close.

`OpenPosition` has `.symbol, .setup, .side, .qty, .stop_order_id, .target_order_id`. `PositionBook.all()` returns every position, so filter by `pos.setup != OpeningDriveSetup.name` — the book may hold other strategies' positions.

- [ ] **Step 1: Write the failing tests**

```python
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
            high=max(105.0, close + 1), low=102.0, close=close,
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_opening_drive_loop.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'scheduler.opening_drive_loop'`

- [ ] **Step 3: Implement the loop**

Create `scheduler/opening_drive_loop.py`:

```python
"""Opening Drive daily lifecycle orchestrator.

Times below are America/New_York; internal timestamps are timezone-aware UTC.

| Local time  | Action                                                    |
|-------------|-----------------------------------------------------------|
| 16:10 (D-1) | Baseline refresh (post-close, so it never competes)       |
| 09:00       | Boot, validate broker, baseline staleness fallback        |
| 09:30-10:00 | Opening range forms — system idle, no requests issued     |
| 10:00       | Cut: one bulk bars request, gates, rank, build setups     |
| 10:00-11:00 | Entry window: 1-min bars for watchlist symbols only       |
| 11:00       | Window closes; un-triggered setups expire                |
| 11:00-15:30 | Managed phase via PositionManager on 5-min bars           |
| 15:30       | EOD flat — cancel OCO children, then market-close all     |

Unlike gap_and_go there is no snapshot polling loop: the entire opening range
arrives in a single bulk request at 10:00, which is cheaper and immune to the
partial-state bugs that missed polls cause.

Each phase method is independently testable — no sleeps, no network inside
the phase methods themselves.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone

import pytz

from core.asset_class import AssetClassConfig
from core.bar import Bar
from core.session import SessionContext
from state.position_book import PositionBook
from strategies.opening_drive_scanner import OpeningDriveScanner, ScanResult
from strategies.setup_opening_drive import OpeningDriveSetup

logger = logging.getLogger(__name__)

_NY_TZ = pytz.timezone("America/New_York")


def _ny_dt(day: datetime, hh: int, mm: int) -> datetime:
    """Build a timezone-aware UTC datetime from an NY date + local HH:MM."""
    ny_date = day.astimezone(_NY_TZ).date()
    naive = datetime.combine(ny_date, time(hh, mm))
    return _NY_TZ.localize(naive).astimezone(timezone.utc)


@dataclass
class OpeningDriveConfig:
    """The subset of YAML settings this loop consumes."""
    universe_path: str
    baselines_path: str
    baselines_max_age_days: int = 7
    or_minutes: int = 30
    entry_window_minutes: int = 60
    volume_confirm_mult: float = 2.0
    target_R: float = 2.0
    min_stop_atr_frac: float = 0.15
    atr_mult_stop_cap: float = 2.0
    max_concurrent_positions: int = 5
    candidate_multiplier: float = 1.5
    premarket_bar_timeframe: str = "1Min"
    regular_bar_timeframe: str = "5Min"


@dataclass
class _DayState:
    cut_done: bool = False
    eod_close_done: bool = False
    watchlist: list[ScanResult] = field(default_factory=list)
    setups: dict[str, OpeningDriveSetup] = field(default_factory=dict)
    contexts: dict[str, SessionContext] = field(default_factory=dict)


class OpeningDriveLoop:
    """Coordinates one trading day for the Opening Drive strategy."""

    def __init__(
        self,
        cfg: OpeningDriveConfig,
        scanner: OpeningDriveScanner,
        equity_asset_class: AssetClassConfig,
        alpaca_client,
        alpaca_data,
        risk_manager,
        executor,
        book: PositionBook,
        position_manager,
        strategy_name: str,
    ) -> None:
        self.cfg = cfg
        self.scanner = scanner
        self.equity_asset_class = equity_asset_class
        self.alpaca = alpaca_client
        self.data = alpaca_data
        self.risk_manager = risk_manager
        self.executor = executor
        self.book = book
        self.position_manager = position_manager
        self.strategy_name = strategy_name
        self.day = _DayState()

    # ── Time helpers ────────────────────────────────────────────────────

    def or_window(self, day: datetime) -> tuple[datetime, datetime]:
        start = _ny_dt(day, 9, 30)
        return start, start + timedelta(minutes=self.cfg.or_minutes)

    def cut_time(self, day: datetime) -> datetime:
        return self.or_window(day)[1]

    def entry_window_end(self, day: datetime) -> datetime:
        return self.cut_time(day) + timedelta(
            minutes=self.cfg.entry_window_minutes,
        )

    def eod_close_time(self, day: datetime) -> datetime:
        return _ny_dt(day, 15, 30)

    # ── Prev-close fetch ────────────────────────────────────────────────

    def fetch_prev_closes(
        self, symbols: list[str], now: datetime,
    ) -> dict[str, float]:
        """Prior session's closing price per symbol.

        Fetched fresh at cut time rather than stored on the baseline: a
        skipped baseline refresh would leave a stale prev_close that silently
        corrupts disp_atr and rs_atr.

        Today's partial daily bar must be excluded — a 1Day window touching
        the live session can return one — so bars are filtered to NY dates
        strictly before today's.
        """
        or_start, _ = self.or_window(now)
        today_ny = now.astimezone(_NY_TZ).date()
        bars_by_symbol = self.data.get_bars_multi(
            symbols, "equity", "1Day",
            or_start - timedelta(days=10), or_start,
        )
        out: dict[str, float] = {}
        for sym, bars in bars_by_symbol.items():
            prior = [
                b for b in bars
                if b.ts.astimezone(_NY_TZ).date() < today_ny
            ]
            if prior:
                out[sym] = prior[-1].close
        return out

    # ── Phase: cut ──────────────────────────────────────────────────────

    def run_cut(self, now: datetime) -> list[ScanResult]:
        if self.day.cut_done:
            return self.day.watchlist

        symbols = self.scanner.request_symbols()
        or_start, or_end = self.or_window(now)
        bars_by_symbol = self.data.get_bars_multi(
            symbols, "equity", self.cfg.premarket_bar_timeframe,
            or_start, or_end,
        )
        prev_closes = self.fetch_prev_closes(symbols, now)

        watchlist = self.scanner.run_cut(bars_by_symbol, prev_closes, now)
        self.day.watchlist = watchlist
        deadline = self.entry_window_end(now)

        for r in watchlist:
            or_bars = bars_by_symbol.get(r.symbol) or []
            self.day.setups[r.symbol] = OpeningDriveSetup(
                symbol=r.symbol,
                or_high=r.metrics.or_high,
                or_low=r.metrics.or_low,
                atr_14d=r.metrics.atr_14d,
                avg_minute_volume=r.metrics.or_volume / self.cfg.or_minutes,
                entry_deadline=deadline,
                volume_confirm_mult=self.cfg.volume_confirm_mult,
                target_R=self.cfg.target_R,
                min_stop_atr_frac=self.cfg.min_stop_atr_frac,
                atr_mult_stop_cap=self.cfg.atr_mult_stop_cap,
            )
            # Seed the context with the OR bars so ctx.vwap is SESSION VWAP
            # from 09:30, not the VWAP of post-cut bars only. Without this
            # the setup's VWAP filter is nearly meaningless right after the
            # cut, when the context would hold one or two bars.
            ctx = SessionContext(
                symbol=r.symbol, asset_class=self.equity_asset_class,
            )
            for b in or_bars:
                ctx.ingest(b)
            self.day.contexts[r.symbol] = ctx

        self.day.cut_done = True
        logger.info(
            "OD_CUT_COMPLETE n=%d symbols=%s deadline=%s",
            len(watchlist), [r.symbol for r in watchlist],
            deadline.isoformat(),
        )
        return watchlist

    # ── Phase: entry window ─────────────────────────────────────────────

    def on_bar(self, symbol: str, bar: Bar) -> None:
        """Push one closed 1-min bar through the symbol's setup."""
        ctx = self.day.contexts.get(symbol)
        setup = self.day.setups.get(symbol)
        if ctx is None or setup is None:
            return
        ctx.ingest(bar)
        signal = setup.check(ctx)
        if signal is None:
            return
        decision = self.risk_manager.evaluate(signal, ctx, "equity")
        if not decision.approved:
            logger.info("OD_REJECTED symbol=%s reason=%s",
                        symbol, decision.reason)
            return
        logger.info(
            "OD_SIGNAL_FIRED symbol=%s entry=%.4f stop=%.4f target=%.4f",
            symbol, signal.entry, signal.stop, signal.target,
        )
        self.executor.submit(signal, decision, asset_class="equity")

    # ── Phase: managed ──────────────────────────────────────────────────

    def manage_open(self, symbol: str, bar: Bar) -> None:
        ctx = self.day.contexts.get(symbol)
        if ctx is not None:
            ctx.ingest(bar)
        actions = self.position_manager.on_bar(symbol, bar)
        if actions:
            self.executor.handle_actions(actions, asset_class="equity")

    # ── Phase: EOD flat ─────────────────────────────────────────────────

    def force_close_all(self, now: datetime) -> int:
        """Cancel OCO children, then market-close every Opening Drive position.

        Cancelling FIRST is required: OrderExecutor.close_position submits a
        market close but does not touch the bracket legs, so flattening with
        live stop/target orders leaves orphaned orders behind — the failure
        class the reconciler exists to clean up.

        Cancel failures are tolerated: an already-filled or already-cancelled
        leg raises, and that must not prevent the close.
        """
        if self.day.eod_close_done:
            return 0
        closed = 0
        for pos in list(self.book.all()):
            if pos.setup != OpeningDriveSetup.name:
                continue
            for oid in (pos.stop_order_id, pos.target_order_id):
                if not oid:
                    continue
                try:
                    self.alpaca.cancel_order(oid)
                except Exception as exc:
                    logger.warning(
                        "OD_EOD_CANCEL_FAILED symbol=%s order_id=%s error=%s",
                        pos.symbol, oid, exc,
                    )
            try:
                self.executor.close_position(
                    pos.symbol, pos.side, pos.qty,
                    setup=pos.setup, asset_class="equity",
                )
                closed += 1
            except Exception as exc:
                logger.error("OD_EOD_CLOSE_FAILED symbol=%s error=%s",
                             pos.symbol, exc, exc_info=True)
        self.day.eod_close_done = True
        logger.info("OD_EOD_CLOSE_DONE n=%d", closed)
        return closed

    # ── Day reset ───────────────────────────────────────────────────────

    def reset_for_new_day(self) -> None:
        self.day = _DayState()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_opening_drive_loop.py -v`
Expected: 25 passed

- [ ] **Step 5: Run the full suite**

Run: `pytest -q`
Expected: no new failures

- [ ] **Step 6: Commit**

```bash
git add scheduler/opening_drive_loop.py tests/test_opening_drive_loop.py
git commit -m "feat(scheduler): OpeningDriveLoop daily orchestrator

Phase handlers for cut, entry window, managed phase, and EOD flatten, each
testable without network or sleeps.

No snapshot polling loop: the whole opening range arrives in one bulk
request at 10:00, cheaper than gap_and_go's 5-minute polling and immune to
partial-state bugs from missed polls.

Two subtleties pinned by tests:
- SessionContexts are SEEDED with the 09:30-10:00 bars, so ctx.vwap is
  session VWAP rather than the VWAP of post-cut bars only. Unseeded, the
  setup's VWAP filter is nearly meaningless right after the cut.
- force_close_all cancels OCO children BEFORE the market close. close_
  position does not touch bracket legs, so flattening with live stop/target
  orders leaves orphans. Cancel failures are tolerated so an already-filled
  leg cannot block the close.

prev_close is fetched fresh at cut time and filtered to NY dates strictly
before today's, excluding the partial daily bar a live-session 1Day window
can return."
```

---

### Task 10: Baseline builder job

**Files:**
- Create: `scripts/build_opening_drive_baselines.py`
- Test: `tests/test_opening_drive_baselines_build.py`

**Interfaces:**
- Consumes: `AlpacaData.get_bars_multi` (Task 1), `OpeningDriveBaseline` + `save_baselines` (Task 3), `core.atr.atr`
- Produces:
  - `session_dates(spy_daily: list[Bar], now: datetime, n: int) -> list[date]`
  - `build_baselines(data, symbols: list[str], now: datetime, or_minutes: int = 30, lookback_sessions: int = 20, atr_window: int = 14) -> dict[str, OpeningDriveBaseline]`

**Context you need:**

`core.atr.atr(bars, period)` computes ATR and needs at least `period + 1` bars. `gap_and_go_loop.py` calls it as `compute_atr(bars[-15:], 14)`.

**Session dates come from SPY's own daily bars, not from a market calendar.** SPY trades every session the market is open, so the set of dates present in its daily bars *is* the trading calendar for the lookback window. This avoids adding a `pandas_market_calendars` dependency and avoids getting holidays wrong.

Today's date must be excluded — the current session is in progress and its OR volume is exactly what we are trying to compare against.

Cost: one bulk `1Day` request plus `lookback_sessions` bulk `1Min` requests (one per trailing session's 09:30–10:00 window). At 20 sessions that is 21 bulk calls, each covering all ~516 symbols. Runs once daily at 16:10.

- [ ] **Step 1: Write the failing tests**

```python
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
    assert out["AAA"].atr_14d > 0
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_opening_drive_baselines_build.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'scripts.build_opening_drive_baselines'`

- [ ] **Step 3: Implement the builder**

Create `scripts/build_opening_drive_baselines.py`:

```python
"""Build the Opening Drive baselines file. Runs post-close (16:10 NY).

Computes, per symbol:
  - atr_14d               daily ATR(14)
  - avg_or_volume_20d     mean IEX volume in the 09:30-10:00 window over the
                          trailing N sessions -- the denominator of rvol_or
  - avg_daily_volume_20d  mean IEX daily volume (IEX-denominated!)

Cost: one bulk 1Day request plus one bulk 1Min request per trailing session
(21 calls at the default 20-session lookback), each covering all ~516
symbols. Runs once daily.

Usage:
    python scripts/build_opening_drive_baselines.py \
        --config config/settings_opening_drive_equity.yaml
"""
from __future__ import annotations

import argparse
import logging
from datetime import date, datetime, timedelta, timezone

import pytz
import yaml

from core.atr import atr as compute_atr
from core.bar import Bar
from strategies.opening_drive_scanner import (
    OpeningDriveBaseline, load_universe, save_baselines,
)

logger = logging.getLogger(__name__)

_NY_TZ = pytz.timezone("America/New_York")
BENCHMARK = "SPY"


def session_dates(spy_daily: list[Bar], now: datetime, n: int) -> list[date]:
    """The last `n` trading dates strictly before today, from SPY's bars.

    SPY trades every open session, so the dates present in its daily bars ARE
    the trading calendar for this window -- no market-calendar dependency and
    no chance of getting holidays wrong.

    Today is excluded: the current session is in progress, and its opening
    range is exactly what these baselines will be compared against.
    """
    today = now.astimezone(_NY_TZ).date()
    dates = sorted({
        b.ts.astimezone(_NY_TZ).date() for b in spy_daily
        if b.ts.astimezone(_NY_TZ).date() < today
    })
    return dates[-n:] if n > 0 else []


def _or_window_utc(day: date, or_minutes: int) -> tuple[datetime, datetime]:
    start = _NY_TZ.localize(
        datetime.combine(day, datetime.min.time().replace(hour=9, minute=30))
    ).astimezone(timezone.utc)
    return start, start + timedelta(minutes=or_minutes)


def build_baselines(
    data,
    symbols: list[str],
    now: datetime,
    or_minutes: int = 30,
    lookback_sessions: int = 20,
    atr_window: int = 14,
) -> dict[str, OpeningDriveBaseline]:
    """Compute baselines for every symbol with sufficient data.

    Symbols lacking daily bars, ATR history, or any opening-range volume are
    omitted rather than written with zeros -- compute_or_metrics treats a
    zero avg_or_volume_20d as unusable anyway, and omitting makes the gap
    visible in the baseline count.
    """
    daily = data.get_bars_multi(
        symbols, "equity", "1Day", now - timedelta(days=60), now,
    )
    dates = session_dates(daily.get(BENCHMARK) or [], now, lookback_sessions)
    if not dates:
        logger.error("OD_BASELINE_NO_SESSIONS benchmark=%s — cannot build",
                     BENCHMARK)
        return {}

    or_totals: dict[str, list[float]] = {}
    for day in dates:
        start, end = _or_window_utc(day, or_minutes)
        try:
            minute_bars = data.get_bars_multi(
                symbols, "equity", "1Min", start, end,
            )
        except Exception as exc:
            logger.warning("OD_BASELINE_SESSION_FETCH_FAILED day=%s error=%s",
                           day, exc)
            continue
        for sym, bars in minute_bars.items():
            total = sum(b.volume for b in bars)
            if total > 0:
                or_totals.setdefault(sym, []).append(total)

    out: dict[str, OpeningDriveBaseline] = {}
    for sym in symbols:
        bars = daily.get(sym) or []
        if len(bars) < atr_window + 1:
            logger.debug("OD_BASELINE_SKIP symbol=%s reason=insufficient_daily",
                         sym)
            continue
        atr_14d = compute_atr(bars[-(atr_window + 1):], atr_window)
        if atr_14d <= 0:
            logger.debug("OD_BASELINE_SKIP symbol=%s reason=non_positive_atr",
                         sym)
            continue
        recent = bars[-lookback_sessions:]
        adv = sum(b.volume for b in recent) / len(recent)
        totals = or_totals.get(sym) or []
        if not totals:
            logger.debug("OD_BASELINE_SKIP symbol=%s reason=no_or_volume", sym)
            continue
        out[sym] = OpeningDriveBaseline(
            atr_14d=atr_14d,
            avg_or_volume_20d=sum(totals) / len(totals),
            avg_daily_volume_20d=adv,
            computed_at=now,
        )

    logger.info("OD_BASELINES_BUILT symbols=%d of %d requested",
                len(out), len(symbols))
    return out


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config/settings_opening_drive_equity.yaml")
    args = ap.parse_args()

    with open(args.config) as f:
        cfg = yaml.safe_load(f)
    scan = cfg["scanner"]

    from broker.alpaca_client import AlpacaClient
    from broker.alpaca_data import AlpacaData

    universe = load_universe(scan["universe_file"])
    symbols = sorted(universe) + [BENCHMARK]

    client = AlpacaClient(asset_class="equity")
    data = AlpacaData(client, cache_dir="runtime/bars_cache")

    baselines = build_baselines(
        data, symbols, datetime.now(timezone.utc),
        or_minutes=scan.get("or_minutes", 30),
        lookback_sessions=scan.get("lookback_sessions", 20),
    )
    if not baselines:
        logger.error("OD_BASELINES_EMPTY — not overwriting existing file")
        return 1
    save_baselines(baselines, scan["baselines_path"])
    logger.info("OD_BASELINES_SAVED path=%s n=%d",
                scan["baselines_path"], len(baselines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Ensure `scripts/` is importable**

Run: `ls scripts/__init__.py`
Expected: the file exists (it does). If it did not, `from scripts.build_opening_drive_baselines import ...` would fail.

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_opening_drive_baselines_build.py -v`
Expected: 11 passed

- [ ] **Step 6: Commit**

```bash
git add scripts/build_opening_drive_baselines.py tests/test_opening_drive_baselines_build.py
git commit -m "feat(scripts): Opening Drive baseline builder

Computes daily ATR(14), IEX-denominated ADV, and mean 09:30-10:00 IEX
volume over the trailing 20 sessions -- the last being the denominator
that makes rvol_or meaningful on a 2%-share feed.

Trading dates come from SPY's own daily bars rather than a market
calendar: SPY trades every open session, so its bar dates ARE the
calendar, with no extra dependency and no chance of mishandling holidays.
Today is excluded, since the live session's opening range is what these
baselines exist to be compared against.

Symbols lacking data are omitted rather than written with zeros, and an
empty result refuses to overwrite the existing baselines file."
```

---

### Task 11: Config file and production wiring

**Files:**
- Create: `config/settings_opening_drive_equity.yaml`
- Create: `main_opening_drive.py`
- Test: `tests/test_opening_drive_wiring.py`

**Interfaces:**
- Consumes: everything from Tasks 1–10
- Produces: `load_config(path) -> dict`, `build_pipeline(cfg, sector_map, alpaca=None, mysql=None, strategy_id=None) -> FilterPipeline`, `build_loop(cfg, logger) -> tuple[OpeningDriveLoop, RiskManager, AlpacaClient]`, `run_day(loop, ...)`, `main()`

**Context you need — read `main_gap_and_go.py:60-215` first.** Mirror its `_build_pipeline` / `_build_loop` / `run_day` / `main` structure, including `MySQLStore(strategy_name=...)` + `ensure_schema()` + `upsert_strategy()`, `mysql.load_open_positions()` with a `PositionBook()` fallback, the `ALPACA_ACCOUNT_BOUND` log line with a masked account number, and the `_order_status_for` / `_on_fill_confirmed` callbacks passed to `PositionManager`.

**Four deltas from the gap-and-go wiring — these are the whole point of this task:**

1. **`SectorExposureFilter` must be in the pipeline**, constructed with the sector map from `load_universe` and `setup_name="opening_drive"`.
2. **`loss_filter_scope` must be `system_wide`.** ⚠️ The spec says "per_strategy"; **that scope does not exist in the code.** `ConsecutiveLossFilter.check` branches on `scope == "system_wide"` (using `ledger.consec_losses_system`) and treats every other value as per-symbol. `system_wide` is what delivers the spec's intent — losses counted across differing symbols, since this strategy rotates its universe daily. Using `per_strategy` would silently fall through to per-symbol and never fire.
3. **`run_day` MUST call `risk_manager.update_cash(...)`** from the account's `non_marginable_buying_power` (falling back to `cash`), exactly as `main.py:604` does. `main_gap_and_go.py` omits this; that omission leaves `available_cash=None` and disables the notional cap that this strategy depends on.
4. **`check_pdt_headroom` runs at boot**, before any trading, gated on `risk.pdt_guard_enabled`.

- [ ] **Step 1: Write the config file**

Create `config/settings_opening_drive_equity.yaml`:

```yaml
system:
  name: opening_drive_equity_trader
  trading_env: paper
  version: 1.0.0

broker:
  handshake_symbol: SPY
  paper_trading: true

asset_classes:
  equity:
    timezone: America/New_York
    session_open_local: '09:30'
    session_close_local: '16:00'
    cut_local: '10:00'
    entry_window_end_local: '11:00'
    force_close_local: '15:30'
    commission_per_share: 0.0
    slippage_bps: 2
    # symbols list intentionally absent — populated dynamically by the scanner

scanner:
  universe_file: config/universe_sp500_ndx100.csv
  baselines_path: runtime/opening_drive/baselines.json
  baselines_max_age_days: 7
  or_minutes: 30
  lookback_sessions: 20
  benchmark: SPY
  side: long_only
  filters:
    min_price: 5.0
    # IEX-DENOMINATED — do NOT raise this to 1_000_000. That is a
    # consolidated-volume figure; against IEX volume (~2% of consolidated)
    # it would reject substantially the entire universe, every day, and the
    # scanner would return nothing without erroring. See gap_and_go, which
    # carries the consolidated figure.
    min_avg_daily_volume: 100_000
    min_bar_coverage: 0.90
    min_rvol_or: 2.0
    min_disp_atr: 0.5
    min_or_width_atr: 0.4
    max_or_width_atr: 2.0
    min_clv: 0.6
    min_rs_atr: 0.0
  ranking:
    score: rvol_or_x_rs_atr
    candidate_multiplier: 1.5

setups:
  opening_drive:
    enabled: true
    volume_confirm_mult: 2.0
    target_R: 2.0
    min_stop_atr_frac: 0.15
    atr_mult_stop_cap: 2.0
    entry_window_minutes: 60

position_management:
  breakeven_at_R: 1.0
  trail_at_R: 1.5
  trail_atr: 1.0
  # 5-min bars counted from the first managed-phase bar at 11:00, NOT from
  # entry. 11:00→15:30 is 54 bars, so any value >= 54 would be inert.
  max_hold_bars: 36
  force_close_local: '15:30'

risk:
  max_concurrent_positions: 5
  max_risk_per_trade: 0.005
  # LOAD-BEARING LIMIT. sma_slope_equity_trader holds 60% of account notional,
  # leaving ~40% here; 5 positions x 0.07 = 35%. Intraday stops run 1-2% of
  # price, so this cap binds BEFORE max_risk_per_trade does — effective risk
  # is ~0.1% per trade, ~0.5% per day.
  max_notional_per_trade_pct: 0.07
  # INERT given the notional cap above; retained for config-shape consistency
  # with the other strategies. Do not mistake it for active protection.
  max_daily_risk_open: 0.025
  max_per_sector: 2
  consecutive_loss_limit: 2
  # system_wide, NOT per_symbol: this strategy rotates symbols daily and would
  # essentially never see the same name twice, so per-symbol counting would
  # never fire and would be dead config offering false comfort.
  loss_filter_scope: system_wide
  # MUST be true before any live deployment. Paper accounts do not enforce
  # PDT, so this guard is the only thing standing between a sub-$25k margin
  # account and a 90-day closing-only restriction.
  pdt_guard_enabled: false
  pdt_min_equity: 25000

scheduler:
  bar_timeframe: 1Min
  regular_session_timeframe: 5Min
  poll_fallback_seconds: 30
  wake_grace_seconds: 5

logging:
  level: INFO
  log_file: logs/opening_drive_equity_trader.log

news_blackouts: []
```

- [ ] **Step 2: Write the failing wiring tests**

```python
# tests/test_opening_drive_wiring.py
import yaml

from main_opening_drive import build_pipeline, load_config
from risk.filters import (
    ConcurrentPositionFilter, ConsecutiveLossFilter, RiskBudgetFilter,
    SectorExposureFilter,
)

CONFIG_PATH = "config/settings_opening_drive_equity.yaml"
SECTORS = {"AAA": "Tech", "BBB": "Energy"}


def test_config_parses():
    cfg = load_config(CONFIG_PATH)
    assert cfg["system"]["name"] == "opening_drive_equity_trader"
    assert cfg["setups"]["opening_drive"]["enabled"] is True


def test_config_declares_equity_asset_class_without_static_symbols():
    """The dashboard discovers strategies by this block; symbols are dynamic."""
    cfg = load_config(CONFIG_PATH)
    assert "equity" in cfg["asset_classes"]
    assert "crypto" not in cfg["asset_classes"]
    assert "symbols" not in cfg["asset_classes"]["equity"]


def test_config_adv_gate_is_iex_denominated():
    """Regression guard: 1_000_000 is a consolidated figure and would reject
    the whole universe on an IEX feed."""
    cfg = load_config(CONFIG_PATH)
    assert cfg["scanner"]["filters"]["min_avg_daily_volume"] == 100_000


def test_config_loss_scope_is_system_wide():
    """per_symbol would never fire — this strategy rotates symbols daily.
    'per_strategy' is not a scope ConsecutiveLossFilter understands."""
    cfg = load_config(CONFIG_PATH)
    assert cfg["risk"]["loss_filter_scope"] == "system_wide"


def test_config_risk_numbers_match_the_capital_split():
    cfg = load_config(CONFIG_PATH)["risk"]
    assert cfg["max_concurrent_positions"] == 5
    assert cfg["max_notional_per_trade_pct"] == 0.07
    assert cfg["max_per_sector"] == 2
    # 5 positions must fit inside the ~40% left by sma_slope's 60%
    assert cfg["max_concurrent_positions"] * cfg["max_notional_per_trade_pct"] <= 0.40


def test_config_max_hold_bars_is_not_inert():
    """11:00->15:30 is 54 five-minute bars; >= 54 would never fire."""
    assert load_config(CONFIG_PATH)["position_management"]["max_hold_bars"] < 54


def test_pipeline_includes_sector_exposure_filter():
    cfg = load_config(CONFIG_PATH)
    pipeline = build_pipeline(cfg, SECTORS)
    sector = [f for f in pipeline.filters if isinstance(f, SectorExposureFilter)]
    assert len(sector) == 1
    assert sector[0].max_per_sector == 2
    assert sector[0].setup_name == "opening_drive"
    assert sector[0].sector_map == SECTORS


def test_pipeline_consecutive_loss_scope_is_system_wide():
    cfg = load_config(CONFIG_PATH)
    pipeline = build_pipeline(cfg, SECTORS)
    clf = next(f for f in pipeline.filters
               if isinstance(f, ConsecutiveLossFilter))
    assert clf.scope == "system_wide"
    assert clf.limit == 2


def test_pipeline_includes_concurrency_and_risk_budget():
    cfg = load_config(CONFIG_PATH)
    pipeline = build_pipeline(cfg, SECTORS)
    cpf = next(f for f in pipeline.filters
               if isinstance(f, ConcurrentPositionFilter))
    assert cpf.max_concurrent == 5
    assert any(isinstance(f, RiskBudgetFilter) for f in pipeline.filters)


def test_pipeline_omits_broker_filters_when_not_supplied():
    cfg = load_config(CONFIG_PATH)
    pipeline = build_pipeline(cfg, SECTORS, alpaca=None, mysql=None)
    names = {f.name for f in pipeline.filters}
    assert "broker_position" not in names
    assert "manual_close_cooldown" not in names
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `pytest tests/test_opening_drive_wiring.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'main_opening_drive'`

- [ ] **Step 4: Write `main_opening_drive.py`**

Open `main_gap_and_go.py` and mirror its structure. Write the pipeline builder and the boot sequence exactly as below; take `MySQLStore` setup, the `PositionBook` fallback, the account-bound logging, the `PositionManager` callbacks, and the signal-handling / `run_day` scheduling shape from the reference file.

```python
"""Opening Drive equity trader — production entry point.

Daily lifecycle (America/New_York):
    09:00        boot, PDT guard, baseline staleness fallback
    09:30-10:00  opening range forms; no requests issued
    10:00        scanner cut -> watchlist -> armed setups
    10:00-11:00  1-min bars on watchlist symbols; entry on trigger
    11:00-15:30  managed phase on 5-min bars
    15:30        cancel OCO children, then flatten everything

Structure mirrors main_gap_and_go.py. Four deliberate differences:
  1. SectorExposureFilter is in the pipeline (portfolio concentration).
  2. loss_filter_scope is system_wide -- symbols rotate daily, so per-symbol
     counting would never fire.
  3. update_cash IS called. main_gap_and_go omits it, which leaves
     available_cash None and disables the notional cap this strategy depends
     on for its capital split with sma_slope.
  4. The PDT guard runs before any trading.
"""
from __future__ import annotations

import argparse
import logging
import os
from datetime import datetime, timezone

import yaml

from broker.alpaca_client import AlpacaClient
from broker.alpaca_data import AlpacaData
from broker.order_executor import OrderExecutor
from core.asset_class import AssetClassConfig
from core.position_manager import PositionManager
from risk.filters import (
    BrokerPositionFilter, ConcurrentPositionFilter, ConsecutiveLossFilter,
    FilterPipeline, ManualCloseCooldownFilter, NewsBlackoutFilter,
    RiskBudgetFilter, SectorExposureFilter,
)
from risk.manager import RiskManager
from risk.pdt_guard import check_pdt_headroom
from risk.sizing import SizingConfig
from scheduler.opening_drive_loop import OpeningDriveConfig, OpeningDriveLoop
from state.daily_ledger import DailyLedger
from state.mysql_store import MySQLStore
from state.position_book import PositionBook
from strategies.opening_drive_scanner import (
    OpeningDriveFilters, OpeningDriveScanner, load_baselines, load_universe,
)

logger = logging.getLogger(__name__)


def load_config(path: str) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def build_pipeline(cfg: dict, sector_map: dict[str, str], alpaca=None,
                   mysql=None, strategy_id: int | None = None) -> FilterPipeline:
    """Entry-filter pipeline.

    SectorExposureFilter is what makes 'top-5 at full risk each' mean five
    independent risks rather than one leveraged sector bet.

    ConsecutiveLossFilter uses scope 'system_wide': ConsecutiveLossFilter
    branches on that exact string and treats anything else as per-symbol.
    Since this strategy rotates its symbols daily, per-symbol counting would
    never reach the limit.
    """
    filters = [
        NewsBlackoutFilter(windows=[], pad_min=5),
        ConsecutiveLossFilter(
            limit=cfg["risk"]["consecutive_loss_limit"],
            scope=cfg["risk"]["loss_filter_scope"],
        ),
        ConcurrentPositionFilter(
            max_concurrent=cfg["risk"]["max_concurrent_positions"],
        ),
        SectorExposureFilter(
            sector_map=sector_map,
            max_per_sector=cfg["risk"]["max_per_sector"],
            setup_name="opening_drive",
        ),
    ]
    if alpaca is not None:
        filters.append(BrokerPositionFilter(
            broker=alpaca,
            cache_ttl_s=float(os.environ.get("BROKER_POSITION_FILTER_TTL_S", "30")),
        ))
    if mysql is not None and strategy_id is not None:
        filters.append(ManualCloseCooldownFilter(
            store=mysql, strategy_id=strategy_id,
            cache_ttl_s=float(os.environ.get("MANUAL_CLOSE_CACHE_TTL_S", "30")),
        ))
    filters.append(RiskBudgetFilter(
        daily_open_risk_cap_pct=cfg["risk"]["max_daily_risk_open"],
    ))
    return FilterPipeline(filters)


def refresh_equity_and_cash(alpaca, risk_manager) -> None:
    """Push broker equity AND available cash into the risk manager.

    The update_cash call is REQUIRED, not optional. size_position caps every
    position at available_cash * (1 - cash_buffer_pct); with available_cash
    None that cap is skipped and sizing silently falls back to equity alone,
    ignoring that sma_slope is holding 60% of the account. main_gap_and_go.py
    omits this call -- do not copy that omission.
    """
    account = alpaca.get_account()
    equity = float(account.get("equity") or account.get("portfolio_value") or 0)
    if equity > 0:
        risk_manager.update_equity(equity)
    cash_raw = (account.get("non_marginable_buying_power")
                or account.get("cash"))
    risk_manager.update_cash(float(cash_raw) if cash_raw is not None else None)


def build_loop(cfg: dict, log: logging.Logger):
    """Wire the loop. Returns (loop, risk_manager, alpaca_client)."""
    system_name = cfg["system"]["name"]

    eq_raw = cfg["asset_classes"]["equity"]
    eq_cfg = AssetClassConfig(
        name="equity", timezone=eq_raw["timezone"],
        session_open_local=eq_raw["session_open_local"],
        session_close_local=eq_raw["session_close_local"],
        opening_blackout_min=0,
        bar_timeframe=cfg["scheduler"]["bar_timeframe"],
        slippage_bps=eq_raw.get("slippage_bps", 0.0),
        commission_per_share=eq_raw.get("commission_per_share", 0.0),
        commission_bps=eq_raw.get("commission_bps", 0.0),
    )

    scan_cfg = cfg["scanner"]
    setup_cfg = cfg["setups"]["opening_drive"]
    universe = load_universe(scan_cfg["universe_file"])
    baselines = load_baselines(scan_cfg["baselines_path"])
    scanner = OpeningDriveScanner(
        universe=universe,
        baselines=baselines,
        filters=OpeningDriveFilters(**scan_cfg["filters"]),
        max_concurrent_positions=cfg["risk"]["max_concurrent_positions"],
        candidate_multiplier=scan_cfg["ranking"]["candidate_multiplier"],
        baselines_max_age_days=scan_cfg["baselines_max_age_days"],
        or_minutes=scan_cfg["or_minutes"],
    )

    od_cfg = OpeningDriveConfig(
        universe_path=scan_cfg["universe_file"],
        baselines_path=scan_cfg["baselines_path"],
        baselines_max_age_days=scan_cfg["baselines_max_age_days"],
        or_minutes=scan_cfg["or_minutes"],
        entry_window_minutes=setup_cfg["entry_window_minutes"],
        volume_confirm_mult=setup_cfg["volume_confirm_mult"],
        target_R=setup_cfg["target_R"],
        min_stop_atr_frac=setup_cfg["min_stop_atr_frac"],
        atr_mult_stop_cap=setup_cfg["atr_mult_stop_cap"],
        max_concurrent_positions=cfg["risk"]["max_concurrent_positions"],
        candidate_multiplier=scan_cfg["ranking"]["candidate_multiplier"],
        premarket_bar_timeframe=cfg["scheduler"]["bar_timeframe"],
        regular_bar_timeframe=cfg["scheduler"]["regular_session_timeframe"],
    )

    alpaca = AlpacaClient(asset_class="equity")
    data = AlpacaData(alpaca, cache_dir="runtime/bars_cache")

    account = alpaca.get_account()
    # PDT guard BEFORE anything else touches the market. Failing to start is
    # far cheaper than a 90-day closing-only restriction discovered mid-session.
    check_pdt_headroom(
        account,
        min_equity=float(cfg["risk"].get("pdt_min_equity", 25_000)),
        enabled=bool(cfg["risk"].get("pdt_guard_enabled", True)),
    )

    mysql = MySQLStore(strategy_name=system_name, logger=log)
    try:
        mysql.ensure_schema()
        mysql.upsert_strategy()
    except Exception as exc:
        log.error("MYSQL_INIT_FAILED %s — continuing without persistence", exc)
        mysql = None

    book = mysql.load_open_positions() if mysql else None
    if book is None:
        book = PositionBook()

    initial_equity = float(
        account.get("equity") or account.get("portfolio_value") or 0
    )
    if initial_equity <= 0:
        raise SystemExit("Account returned non-positive equity; aborting")
    _acct = str(account.get("account_number") or "")
    log.info(
        "ALPACA_ACCOUNT_BOUND asset_class=equity account_number=%s "
        "equity=%.2f base_url=%s",
        f"{_acct[:4]}***" if len(_acct) >= 4 else "***",
        initial_equity, alpaca.base_url,
    )
    ledger = DailyLedger(initial_equity=initial_equity)

    pipeline = build_pipeline(
        cfg, sector_map=universe, alpaca=alpaca, mysql=mysql,
        strategy_id=mysql.strategy_id if mysql is not None else None,
    )
    sizing = SizingConfig(
        max_risk_per_trade=cfg["risk"]["max_risk_per_trade"],
        max_notional_per_trade_pct=cfg["risk"]["max_notional_per_trade_pct"],
        allow_fractional=False,
    )
    risk_manager = RiskManager(
        pipeline=pipeline, sizing_equity=sizing, sizing_crypto=sizing,
        ledger=ledger, book=book,
    )
    executor = OrderExecutor(alpaca, book, strategy_name=system_name,
                             logger=log, mysql_store=mysql)

    def _order_status_for(pos):
        if not pos.order_id:
            return None
        order = alpaca.get_order(pos.order_id)
        return order.get("status") if isinstance(order, dict) else None

    def _on_fill_confirmed(pos):
        if mysql is not None:
            mysql.mark_fill_confirmed(mysql.strategy_id, pos.symbol, pos.setup)

    pm = PositionManager(
        book,
        max_hold_bars=cfg["position_management"]["max_hold_bars"],
        breakeven_at_R=cfg["position_management"]["breakeven_at_R"],
        order_status_for=_order_status_for,
        on_fill_confirmed=_on_fill_confirmed,
    )

    loop = OpeningDriveLoop(
        cfg=od_cfg, scanner=scanner, equity_asset_class=eq_cfg,
        alpaca_client=alpaca, alpaca_data=data,
        risk_manager=risk_manager, executor=executor, book=book,
        position_manager=pm, strategy_name=system_name,
    )
    # Seed cash/equity immediately so the very first sizing call is correct.
    refresh_equity_and_cash(alpaca, risk_manager)
    return loop, risk_manager, alpaca


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config",
                    default="config/settings_opening_drive_equity.yaml")
    args = ap.parse_args()
    cfg = load_config(args.config)

    logging.basicConfig(
        level=getattr(logging, cfg["logging"]["level"]),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    loop, risk_manager, alpaca = build_loop(cfg, logger)
    logger.info("OPENING_DRIVE_BOOTED strategy=%s universe=%d",
                cfg["system"]["name"], len(loop.scanner.universe))

    # Day driver: mirror main_gap_and_go.py's run_day scheduling shape —
    # sleep to each phase boundary, call the phase method, then continue.
    # Phase order per day: refresh cash/equity -> run_cut(10:00) ->
    # on_bar per watchlist symbol (10:00-11:00, 1Min) ->
    # manage_open per open symbol (11:00-15:30, 5Min) ->
    # force_close_all(15:30) -> reset_for_new_day().
    # Call refresh_equity_and_cash(alpaca, risk_manager) at the start of each
    # phase transition so sizing tracks sma_slope's changing notional.
    run_day(loop, risk_manager, alpaca)


if __name__ == "__main__":
    main()
```

Implement `run_day(loop, risk_manager, alpaca)` following `main_gap_and_go.py:235-299`: compute the day's phase boundaries from `loop.cut_time`, `loop.entry_window_end`, and `loop.eod_close_time`; sleep to each; fetch bars for the watchlist with `loop.data.get_bars_multi` at each bar boundary; and call `refresh_equity_and_cash` before `run_cut` and once per managed-phase bar.

- [ ] **Step 5: Wire the 16:10 baseline refresh as an in-process phase**

Spec 5 puts this **inside the trader process**, not in a separate compose
service or cron entry: the process is already running and already holds a
broker connection, so a scheduled service would be added surface for no gain.
Task 10's `main()` stays available for manual and backfill runs.

Add to `main_opening_drive.py`:

```python
def refresh_baselines_post_close(loop, now: datetime) -> int:
    """Post-close (16:10 NY) baseline rebuild. Returns symbols written.

    Runs in-process per spec 5. An empty result does NOT overwrite the
    existing file -- a transient data outage must not erase good baselines
    and leave the next cut with nothing to screen against.
    """
    from scripts.build_opening_drive_baselines import build_baselines

    symbols = loop.scanner.request_symbols()
    built = build_baselines(
        loop.data, symbols, now,
        or_minutes=loop.cfg.or_minutes,
        lookback_sessions=loop.baseline_lookback_sessions,
    )
    if not built:
        logger.error("OD_BASELINE_REFRESH_EMPTY — keeping existing file")
        return 0
    from strategies.opening_drive_scanner import save_baselines
    save_baselines(built, loop.cfg.baselines_path)
    loop.scanner.baselines = built      # live-reload; no restart required
    logger.info("OD_BASELINE_REFRESH_DONE n=%d path=%s",
                len(built), loop.cfg.baselines_path)
    return len(built)
```

`loop.baseline_lookback_sessions` does not exist yet — add it to
`OpeningDriveConfig` in `scheduler/opening_drive_loop.py` as
`lookback_sessions: int = 20`, set it from `scan_cfg["lookback_sessions"]` in
`build_loop`, and read it as `loop.cfg.lookback_sessions` (correcting the
attribute reference above).

In `run_day`, schedule this at 16:10 NY after the 15:30 flatten and before
`reset_for_new_day()`.

- [ ] **Step 6: Add the baseline-refresh test**

Append to `tests/test_opening_drive_wiring.py`:

```python
from unittest.mock import MagicMock, patch

from main_opening_drive import refresh_baselines_post_close


def _loop_stub(baselines_path: str):
    loop = MagicMock()
    loop.scanner.request_symbols.return_value = ["AAA", "SPY"]
    loop.cfg.or_minutes = 30
    loop.cfg.lookback_sessions = 20
    loop.cfg.baselines_path = baselines_path
    return loop


def test_empty_rebuild_does_not_overwrite_existing_baselines(tmp_path):
    """A transient data outage must not erase good baselines and leave the
    next cut with nothing to screen against."""
    path = tmp_path / "baselines.json"
    path.write_text('{"AAA": {"atr_14d": 1.0, "avg_or_volume_20d": 1.0,'
                    ' "avg_daily_volume_20d": 1.0,'
                    ' "computed_at": "2026-08-27T20:10:00Z"}}')
    before = path.read_text()
    loop = _loop_stub(str(path))
    with patch("scripts.build_opening_drive_baselines.build_baselines",
               return_value={}):
        assert refresh_baselines_post_close(loop, NOW_UTC) == 0
    assert path.read_text() == before


def test_successful_rebuild_writes_and_live_reloads(tmp_path):
    path = tmp_path / "baselines.json"
    loop = _loop_stub(str(path))
    built = {"AAA": BASELINE_FIXTURE}
    with patch("scripts.build_opening_drive_baselines.build_baselines",
               return_value=built):
        assert refresh_baselines_post_close(loop, NOW_UTC) == 1
    assert path.exists()
    assert loop.scanner.baselines == built     # no restart required
```

Add at the top of the test file:

```python
from datetime import datetime, timedelta, timezone

from strategies.opening_drive_scanner import OpeningDriveBaseline

NOW_UTC = datetime(2026, 8, 28, 20, 10, tzinfo=timezone.utc)   # 16:10 NY
BASELINE_FIXTURE = OpeningDriveBaseline(
    atr_14d=4.0, avg_or_volume_20d=50_000.0,
    avg_daily_volume_20d=400_000.0,
    computed_at=NOW_UTC - timedelta(days=1),
)
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `pytest tests/test_opening_drive_wiring.py -v`
Expected: 12 passed

- [ ] **Step 8: Verify the strategy is discoverable by the dashboard**

Run: `python -c "from ui.data.strategy_configs import list_by_asset_class; print(list_by_asset_class('equity'))"`
Expected: the list includes `opening_drive_equity_trader`

- [ ] **Step 9: Run the full suite**

Run: `pytest -q`
Expected: no new failures

- [ ] **Step 10: Commit**

```bash
git add config/settings_opening_drive_equity.yaml main_opening_drive.py tests/test_opening_drive_wiring.py
git commit -m "feat: Opening Drive config and production wiring

Mirrors main_gap_and_go.py with four deliberate differences:

1. SectorExposureFilter in the pipeline, so top-5-at-full-risk means five
   independent risks rather than one leveraged sector bet.
2. loss_filter_scope is system_wide. The spec said 'per_strategy', which is
   NOT a scope ConsecutiveLossFilter understands -- it branches on
   'system_wide' and treats anything else as per-symbol, which would never
   fire for a strategy that rotates symbols daily.
3. update_cash IS called on every refresh. main_gap_and_go omits it, leaving
   available_cash None and disabling the notional cap this strategy depends
   on for its capital split with sma_slope.
4. PDT guard runs at boot, before anything touches the market.

Config comments record which limits are load-bearing (notional cap) and
which are inert (max_daily_risk_open), so the inert ones are not mistaken
for active protection."
```

---

### Task 12: Deployment — Docker, capital split, dashboard regression

**Files:**
- Modify: `config/settings_sma_slope_equity.yaml` (`max_notional_per_trade_pct`)
- Modify: `docker-compose.yml`
- Test: `tests/test_dashboard_dynamic_symbols.py`
- Test: `tests/test_capital_split.py`

**Interfaces:**
- Consumes: `ui.data.strategy_configs.load_yaml_configs` / `list_by_asset_class`; `risk.sizing.size_position`

**Context you need:** `docker-compose.yml` is in single-bot mode — only `trader-sma-slope-equity` is active; ten other traders are written but commented. This service ships **active**, so two traders share one Alpaca equity account. That is why the `sma_slope` notional reduction is part of *this* task: shipping the service without it means Opening Drive sizes to zero on every day sma-slope is long, silently.

Copy the `trader-sma-slope-equity` service block for volumes, env, restart policy, and `depends_on`, changing only the name and command.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_capital_split.py
"""Guards the two-strategy capital split on one Alpaca account.

sma_slope holds its position for days. If it takes 95% of notional, the
account's non_marginable_buying_power goes near zero, size_position returns
qty=0, and Opening Drive silently takes no trades while appearing to find no
candidates. These tests pin the split that prevents that.
"""
import yaml

from risk.sizing import SizingConfig, size_position

SMA = "config/settings_sma_slope_equity.yaml"
OD = "config/settings_opening_drive_equity.yaml"


def _risk(path: str) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)["risk"]


def test_sma_slope_leaves_headroom():
    assert _risk(SMA)["max_notional_per_trade_pct"] == 0.60


def test_combined_notional_fits_in_the_account():
    sma = _risk(SMA)
    od = _risk(OD)
    combined = (
        sma["max_notional_per_trade_pct"] * sma["max_concurrent_positions"]
        + od["max_notional_per_trade_pct"] * od["max_concurrent_positions"]
    )
    assert combined <= 0.98, f"combined notional {combined:.2f} overcommits"


def test_opening_drive_sizes_nonzero_with_sma_slope_fully_deployed():
    """The regression this whole split exists to prevent."""
    equity = 100_000.0
    available_cash = equity * 0.40          # sma_slope holding 60%
    od = _risk(OD)
    qty, notional = size_position(
        equity=equity, entry=100.0, stop=98.5,
        cfg=SizingConfig(
            max_risk_per_trade=od["max_risk_per_trade"],
            max_notional_per_trade_pct=od["max_notional_per_trade_pct"],
            allow_fractional=False,
        ),
        available_cash=available_cash,
    )
    assert qty > 0, "Opening Drive sized to zero — the starvation bug"
    assert notional <= equity * od["max_notional_per_trade_pct"] + 1e-6


def test_notional_cap_binds_before_the_risk_cap():
    """Documents which limit is load-bearing: with a 1.5% stop, risk-based
    sizing would ask for ~33% of equity, so the 7% notional cap governs."""
    od = _risk(OD)
    equity = 100_000.0
    cfg = SizingConfig(
        max_risk_per_trade=od["max_risk_per_trade"],
        max_notional_per_trade_pct=od["max_notional_per_trade_pct"],
        allow_fractional=False,
    )
    _, notional = size_position(equity, 100.0, 98.5, cfg,
                                available_cash=equity)
    assert notional <= equity * od["max_notional_per_trade_pct"] + 1e-6
    risk_only_notional = (equity * od["max_risk_per_trade"] / 1.5) * 100.0
    assert risk_only_notional > notional
```

```python
# tests/test_dashboard_dynamic_symbols.py
"""The dashboard discovers strategies by globbing config/settings*.yaml.
Opening Drive has NO static symbols list (the scanner populates it daily), so
this pins that a dynamic-symbol strategy is discovered and rendered without
error rather than being trusted to work by precedent."""
from pathlib import Path

from ui.data.strategy_configs import list_by_asset_class, load_yaml_configs

NAME = "opening_drive_equity_trader"


def test_strategy_is_discovered_from_config_glob():
    assert NAME in load_yaml_configs(Path("config"))


def test_strategy_appears_in_the_equity_list_only():
    assert NAME in list_by_asset_class("equity")
    assert NAME not in list_by_asset_class("crypto")


def test_empty_symbols_parses_to_an_empty_list_not_a_crash():
    cfg = load_yaml_configs(Path("config"))[NAME]
    equity = next(ac for ac in cfg.asset_classes if ac.name == "equity")
    assert equity.symbols == [] or equity.symbols is None


def test_setups_and_risk_are_exposed_to_the_dashboard():
    cfg = load_yaml_configs(Path("config"))[NAME]
    assert [s.name for s in cfg.setups] == ["opening_drive"]
    assert cfg.setups[0].enabled is True
    assert cfg.risk["max_concurrent_positions"] == 5


def test_no_duplicate_strategy_name_conflict():
    """Two YAMLs sharing system.name would make one silently invisible."""
    configs = load_yaml_configs(Path("config"))
    assert configs[NAME].yaml_path.name == "settings_opening_drive_equity.yaml"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_capital_split.py tests/test_dashboard_dynamic_symbols.py -v`
Expected: `test_sma_slope_leaves_headroom` FAILS (`0.95 != 0.60`); dashboard tests may already pass from Task 11.

- [ ] **Step 3: Reduce the sma_slope notional cap**

In `config/settings_sma_slope_equity.yaml`, change `max_notional_per_trade_pct` from `0.95` to `0.60` and replace the surrounding comment:

```yaml
risk:
  # Long-only, single-symbol (TQQQ). Risk params are vestigial for the
  # intraday engine and kept for config-shape compatibility.
  max_risk_per_trade: 0.02
  # Reduced from 0.95 to share the account with opening_drive_equity_trader.
  # This strategy holds TQQQ for days; at 0.95 the account's
  # non_marginable_buying_power went near zero and size_position returned
  # qty=0 for the other strategy, which then silently took no trades while
  # appearing to find no candidates. See
  # docs/superpowers/specs/2026-08-28-opening-drive-equity-design.md §3.
  max_notional_per_trade_pct: 0.60
  max_concurrent_positions: 1
  max_daily_risk_open: 1.0
  consecutive_loss_limit: 999
  loss_filter_scope: per_symbol
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_capital_split.py tests/test_dashboard_dynamic_symbols.py -v`
Expected: 9 passed

- [ ] **Step 5: Add the compose service**

In `docker-compose.yml`, add after the `trader-sma-slope-equity` block, copying its `volumes`, `env_file`, `environment`, `restart`, and `depends_on` verbatim and changing only the name and command:

```yaml
  trader-opening-drive-equity:
    build: .
    container_name: trader-opening-drive-equity
    command: python main_opening_drive.py --config config/settings_opening_drive_equity.yaml
    # Copy volumes / env_file / environment / restart / depends_on exactly from
    # trader-sma-slope-equity above, and add the baselines runtime dir:
    #   - ./runtime:/app/runtime
```

- [ ] **Step 6: Validate the compose file**

Run: `docker compose config --quiet && echo "compose OK"`
Expected: `compose OK`

- [ ] **Step 7: Verify both services are recognised**

Run: `docker compose config --services | grep trader-`
Expected: both `trader-sma-slope-equity` and `trader-opening-drive-equity`

- [ ] **Step 8: Run the full suite**

Run: `pytest -q`
Expected: all pass

- [ ] **Step 9: Commit**

```bash
git add config/settings_sma_slope_equity.yaml docker-compose.yml tests/test_capital_split.py tests/test_dashboard_dynamic_symbols.py
git commit -m "feat(deploy): Opening Drive service and two-strategy capital split

Adds trader-opening-drive-equity ACTIVE alongside trader-sma-slope-equity,
and cuts sma_slope's max_notional_per_trade_pct from 0.95 to 0.60.

The reduction ships in the same commit as the service on purpose. sma_slope
holds TQQQ for days; at 0.95 the account's non_marginable_buying_power sits
near zero, size_position returns qty=0, and Opening Drive would silently
take no trades while appearing to find no candidates. Shipping the service
without the reduction produces a bot that looks healthy and does nothing.

test_capital_split pins the split and the load-bearing limit. Dashboard
tests pin that a strategy with NO static symbols list is discovered and
rendered without error rather than trusted to work by precedent."
```

---

## Post-Implementation Verification

Before declaring the strategy ready, run these and record the output:

- [ ] `pytest -q` — full suite green
- [ ] `python scripts/build_opening_drive_baselines.py --config config/settings_opening_drive_equity.yaml` — confirm the baseline count is >400. A much lower number means the ADV/coverage assumptions need revisiting.
- [ ] **Verify the IEX ADV assumption with real data.** Print `avg_daily_volume_20d` percentiles from the generated `baselines.json`. If the 10th percentile sits far above or below `100_000`, retune `min_avg_daily_volume` — the spec's §10.4 records these as unvalidated priors, and this is the moment to validate them.
- [ ] **Confirm how far back Alpaca's free IEX plan serves 1-min bars** (spec §10.1, unverified). This caps the backtest window; find the real limit before scoping backtest work.
- [ ] Dry-run one cut against a recorded session and inspect the `OD_CUT_DONE` reject histogram. If a single gate accounts for nearly all rejections, that gate is miscalibrated.
- [ ] Confirm `pdt_guard_enabled: true` before any live deployment.

## Deferred to follow-up work (not in this plan)

Per spec §11: the short side, the VWAP-retest trigger variant, rank-weighted sizing, slot reservation for high-ranked candidates, and point-in-time universe constituents. Backtest validation is also out of scope here — it depends on the 1-min history depth answer above.
