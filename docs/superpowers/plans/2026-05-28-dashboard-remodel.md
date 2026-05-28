# Dashboard Remodel Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the Streamlit dashboard with a remodeled, dark-themed, financial-grade UI that exposes per-strategy stats over a chosen period, a unified live-trading tab, and the existing logs/WFO tabs — fronted by an nginx reverse proxy that adds HTTPS (self-signed) and HTTP basic auth.

**Architecture:** A new `nginx` container terminates TLS and enforces basic auth, then proxies to the existing Streamlit `dashboard` container on the internal docker network (no host port exposed). The Streamlit app is restructured into `ui/components`, `ui/tabs`, and `ui/data` modules. Closed-trade analytics come from MySQL `trades`; live open positions come from MySQL `positions`; live prices come from per-strategy `runtime/trading_state_*.json` files (after a one-line extension to add `last_price`).

**Tech Stack:** Python 3.12, Streamlit 1.41, pandas 2.2, plotly 5.24, SQLAlchemy + pymysql (existing), nginx:alpine + apache2-utils, openssl (cert gen), pytest 8.3.

**Spec:** `docs/superpowers/specs/2026-05-28-dashboard-remodel-design.md`

---

## File Structure

**New files:**

- `ui/components/__init__.py`
- `ui/components/period_selector.py` — period preset + custom range, returns `(start_utc, end_utc)`
- `ui/components/kpi_row.py` — row of metric tiles + `format_pnl(value)` helper
- `ui/components/strategy_card.py` — KPI summary card per strategy
- `ui/components/theme.py` — dark mode CSS injection
- `ui/tabs/__init__.py`
- `ui/tabs/strategies_tab.py` — landing cards + per-strategy detail
- `ui/tabs/live_tab.py` — open positions across strategies
- `ui/data/__init__.py`
- `ui/data/db.py` — shared SQLAlchemy engine factory
- `ui/data/trades_repo.py` — closed trades for `(strategy, start, end)`
- `ui/data/positions_repo.py` — open positions across strategies
- `ui/data/state_files.py` — `get_last_price(strategy, symbol)` from `runtime/trading_state_*.json`
- `ui/data/stats.py` — pure KPI/chart helpers from a trades DataFrame
- `.streamlit/config.toml` — dark theme config
- `nginx/Dockerfile`
- `nginx/entrypoint.sh`
- `nginx/nginx.conf`
- `config/.env.example` — documents required env keys including `DASH_USER` / `DASH_PASSWORD`
- `tests/ui/__init__.py`
- `tests/ui/test_stats.py`
- `tests/ui/test_period_selector.py`
- `tests/ui/test_state_files.py`
- `tests/ui/test_trades_repo.py`
- `tests/ui/test_positions_repo.py`
- `tests/integration/test_nginx_auth.sh`

**Moved files:**

- `ui/logs_panel.py` → `ui/tabs/logs_panel.py` (no behavior change; imports updated)

**Modified files:**

- `main.py:291-315` — `_collect_snapshot`: add `last_price` to each per-symbol row.
- `tests/test_dashboard_state.py` — assert `last_price` is round-tripped.
- `ui/dashboard.py` — rewritten; entry point that wires theme + tabs.
- `docker-compose.yml` — add `nginx` service; remove host port from `dashboard` service.
- `.gitignore` — add `nginx/certs/`.

**Files left untouched:**

- `state/mysql_store.py`, `state/schema.sql`, `state/position_book.py`, `state/reconciler.py`
- `ui/wfo/`, `ui/log_reader.py`, `ui/logging_setup.py`
- All strategy code under `strategies/` and runtime under `core/`, `runtime/`

---

## Phase 0 — Producer-side state file extension

The Live Trading tab needs `last_price` per symbol. Currently the snapshot writer only emits `vwap`/`upper`/`lower`. This phase adds the field once at the source.

### Task 0.1: Extend snapshot to include last_price

**Files:**

- Modify: `main.py:291-315`
- Test: `tests/test_dashboard_state.py`

- [ ] **Step 1: Update the existing test to assert last_price is round-tripped**

Edit `tests/test_dashboard_state.py` — add `last_price` to each symbol dict in the existing `test_write_dashboard_state_round_trips_payload` test:

```python
def test_write_dashboard_state_round_trips_payload(tmp_path: Path):
    snap = _snap(
        symbols=[
            {"symbol": "AAPL", "vwap": 100.5, "upper": 101.0, "lower": 100.0,
             "last_price": 100.7, "regime": "Range", "open_position": None},
            {"symbol": "BTC/USD", "vwap": 50_100.0, "upper": 50_300.0, "lower": 49_900.0,
             "last_price": 50_050.0, "regime": "Trend",
             "open_position": {"side": "long", "qty": 0.1,
                               "entry": 50_000, "stop": 49_500,
                               "target": 51_000}},
        ],
        rejects=[
            {"filter": "consecutive_loss", "symbol": "AAPL",
             "ts": "2026-05-14T13:55:00+00:00"},
        ],
    )
    out = tmp_path / "state.json"
    write_dashboard_state(out, snap)
    data = json.loads(out.read_text())
    assert data["equity"] == 100_100.0
    assert data["day_pnl"] == 100.0
    assert data["circuit_level"] == 0
    assert len(data["symbols"]) == 2
    assert data["symbols"][0]["last_price"] == 100.7
    assert data["symbols"][1]["last_price"] == 50_050.0
```

- [ ] **Step 2: Run the test and confirm it passes (DashboardSnapshot.symbols is a free-form dict list)**

Run: `pytest tests/test_dashboard_state.py::test_write_dashboard_state_round_trips_payload -v`
Expected: PASS — `symbols` is `list[dict]` so adding a new key requires no schema change.

- [ ] **Step 3: Modify `_collect_snapshot` in main.py to populate last_price**

Edit `main.py` around lines 293-307. Replace the per-symbol row builder block with:

```python
def _collect_snapshot(symbols, contexts, book, ledger, cb,
                      recent_rejects: list[dict] | None = None) -> DashboardSnapshot:
    rows = []
    for sym, _ in symbols:
        ctx = contexts[sym]
        pos = book.get(sym)
        last_price = ctx.bars[-1].close if ctx.bars else None
        rows.append({
            "symbol": sym,
            "regime": ctx.regime,
            "vwap": None if ctx.bar_count == 0 else ctx.vwap,
            "upper": None if ctx.bar_count == 0 else ctx.upper_band,
            "lower": None if ctx.bar_count == 0 else ctx.lower_band,
            "last_price": last_price,
            "open_position": None if pos is None else {
                "side": pos.side, "qty": pos.qty,
                "entry": pos.entry_px, "stop": pos.stop_px, "target": pos.target_px,
            },
        })
    return DashboardSnapshot(
        timestamp=datetime.now(timezone.utc),
        equity=ledger.equity,
        day_pnl=ledger.day_pnl,
        circuit_level=cb.level,
        symbols=rows,
        recent_filter_rejects=(recent_rejects or [])[-20:],
    )
```

- [ ] **Step 4: Run the full test suite to confirm no regression**

Run: `pytest tests/ -x -q`
Expected: all green (the previous suite had 270/270 passing per project memory; the only change is one new field in a test, which is the assertion we just added).

- [ ] **Step 5: Commit**

```bash
git add main.py tests/test_dashboard_state.py
git commit -m "feat(snapshot): add last_price per symbol for live dashboard"
```

---

## Phase 1 — Pure stats module (TDD)

`ui/data/stats.py` operates on a trades DataFrame and produces KPIs and chart inputs. No Streamlit, no DB — pure functions. Build it test-first.

### Task 1.1: Skeleton + KPI dataclass

**Files:**

- Create: `ui/data/__init__.py`
- Create: `ui/data/stats.py`
- Create: `tests/ui/__init__.py`
- Create: `tests/ui/test_stats.py`

- [ ] **Step 1: Create empty package files**

```bash
mkdir -p ui/data tests/ui
touch ui/data/__init__.py tests/ui/__init__.py
```

- [ ] **Step 2: Write the failing test for `compute_kpis` on an empty DataFrame**

Create `tests/ui/test_stats.py`:

```python
import pandas as pd
import pytest

from ui.data.stats import compute_kpis, KPIs


def _empty_trades_df() -> pd.DataFrame:
    return pd.DataFrame(columns=[
        "opened_at", "closed_at", "symbol", "setup_name", "side", "qty",
        "entry_px", "exit_px", "stop_px", "target_px", "initial_stop_px",
        "pnl_usd", "R_realized", "close_reason", "bars_held",
    ])


def test_compute_kpis_empty_df_returns_zeros():
    kpis = compute_kpis(_empty_trades_df())
    assert kpis.total_pnl == 0.0
    assert kpis.trade_count == 0
    assert kpis.win_rate is None
    assert kpis.avg_win is None
    assert kpis.avg_loss is None
    assert kpis.profit_factor is None
    assert kpis.expectancy_R is None
    assert kpis.max_drawdown == 0.0
    assert kpis.sharpe is None
    assert kpis.avg_bars_held is None
    assert kpis.best_trade is None
    assert kpis.worst_trade is None
```

- [ ] **Step 3: Run the test, verify it fails**

Run: `pytest tests/ui/test_stats.py::test_compute_kpis_empty_df_returns_zeros -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'ui.data.stats'`.

- [ ] **Step 4: Write minimal stats.py with KPIs dataclass and empty-df handling**

Create `ui/data/stats.py`:

```python
"""Pure analytics over a closed-trades DataFrame.

No Streamlit, no DB. Input is a pandas DataFrame matching the schema of
the MySQL `trades` table. Output is a KPIs dataclass plus chart-ready
DataFrames.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd


@dataclass
class KPIs:
    total_pnl: float
    trade_count: int
    win_rate: Optional[float]          # fraction in [0, 1]
    avg_win: Optional[float]
    avg_loss: Optional[float]          # negative
    profit_factor: Optional[float]
    expectancy_R: Optional[float]      # mean R_realized
    max_drawdown: float                # negative or zero, USD
    sharpe: Optional[float]            # daily, annualized * sqrt(252)
    avg_bars_held: Optional[float]
    best_trade: Optional[float]
    worst_trade: Optional[float]


def compute_kpis(df: pd.DataFrame) -> KPIs:
    if df.empty:
        return KPIs(
            total_pnl=0.0, trade_count=0,
            win_rate=None, avg_win=None, avg_loss=None,
            profit_factor=None, expectancy_R=None,
            max_drawdown=0.0, sharpe=None,
            avg_bars_held=None, best_trade=None, worst_trade=None,
        )
    pnl = df["pnl_usd"].astype(float)
    wins = pnl[pnl > 0]
    losses = pnl[pnl < 0]
    gross_win = float(wins.sum())
    gross_loss = float(losses.sum())  # negative
    total_pnl = float(pnl.sum())
    trade_count = int(len(df))
    win_rate = float((pnl > 0).mean())
    avg_win = float(wins.mean()) if len(wins) else None
    avg_loss = float(losses.mean()) if len(losses) else None
    profit_factor = (gross_win / abs(gross_loss)) if gross_loss < 0 else None
    expectancy_R = float(df["R_realized"].astype(float).mean())
    max_drawdown = _max_drawdown(df)
    sharpe = _sharpe(df)
    avg_bars_held = float(df["bars_held"].astype(float).mean())
    best_trade = float(pnl.max())
    worst_trade = float(pnl.min())
    return KPIs(
        total_pnl=total_pnl, trade_count=trade_count, win_rate=win_rate,
        avg_win=avg_win, avg_loss=avg_loss, profit_factor=profit_factor,
        expectancy_R=expectancy_R, max_drawdown=max_drawdown,
        sharpe=sharpe, avg_bars_held=avg_bars_held,
        best_trade=best_trade, worst_trade=worst_trade,
    )


def _max_drawdown(df: pd.DataFrame) -> float:
    """Peak-to-trough drawdown of cumulative PnL, ordered by closed_at."""
    s = df.sort_values("closed_at")["pnl_usd"].astype(float).cumsum()
    if s.empty:
        return 0.0
    peak = s.cummax()
    drawdown = s - peak
    return float(drawdown.min())  # most negative number, or 0 if no drawdown


def _sharpe(df: pd.DataFrame) -> Optional[float]:
    """Daily Sharpe, annualized with sqrt(252). Returns None if insufficient data."""
    daily = (df.assign(d=pd.to_datetime(df["closed_at"]).dt.floor("D"))
               .groupby("d")["pnl_usd"].sum().astype(float))
    if len(daily) < 2:
        return None
    std = daily.std(ddof=1)
    if std == 0 or math.isnan(std):
        return None
    return float(daily.mean() / std * math.sqrt(252))
```

- [ ] **Step 5: Run the test, verify it passes**

Run: `pytest tests/ui/test_stats.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add ui/data/__init__.py ui/data/stats.py tests/ui/__init__.py tests/ui/test_stats.py
git commit -m "feat(stats): KPIs dataclass + compute_kpis on empty df"
```

### Task 1.2: KPI tests on synthetic trade sets

**Files:**

- Modify: `tests/ui/test_stats.py`
- (No code changes to `stats.py` — verifying existing implementation against richer fixtures)

- [ ] **Step 1: Add a fixture builder + tests covering all KPIs**

Append to `tests/ui/test_stats.py`:

```python
from datetime import datetime, timezone, timedelta


def _trades(rows: list[dict]) -> pd.DataFrame:
    """Build a trades DataFrame with sensible defaults; rows override."""
    base = {
        "opened_at": datetime(2026, 5, 1, 14, 0, tzinfo=timezone.utc),
        "closed_at": datetime(2026, 5, 1, 15, 0, tzinfo=timezone.utc),
        "symbol": "AAPL", "setup_name": "vwap_bounce", "side": "long",
        "qty": 1.0, "entry_px": 100.0, "exit_px": 101.0,
        "stop_px": 99.0, "target_px": 102.0, "initial_stop_px": 99.0,
        "pnl_usd": 1.0, "R_realized": 1.0, "close_reason": "target", "bars_held": 5,
    }
    return pd.DataFrame([{**base, **r} for r in rows])


def test_compute_kpis_all_wins():
    df = _trades([
        {"pnl_usd": 100.0, "R_realized": 1.0},
        {"pnl_usd": 200.0, "R_realized": 2.0,
         "closed_at": datetime(2026, 5, 2, 15, 0, tzinfo=timezone.utc)},
    ])
    k = compute_kpis(df)
    assert k.total_pnl == 300.0
    assert k.trade_count == 2
    assert k.win_rate == 1.0
    assert k.avg_win == 150.0
    assert k.avg_loss is None
    assert k.profit_factor is None  # no losses → undefined
    assert k.expectancy_R == 1.5
    assert k.max_drawdown == 0.0
    assert k.best_trade == 200.0
    assert k.worst_trade == 100.0


def test_compute_kpis_all_losses():
    df = _trades([
        {"pnl_usd": -50.0, "R_realized": -1.0},
        {"pnl_usd": -30.0, "R_realized": -0.6,
         "closed_at": datetime(2026, 5, 2, 15, 0, tzinfo=timezone.utc)},
    ])
    k = compute_kpis(df)
    assert k.total_pnl == -80.0
    assert k.win_rate == 0.0
    assert k.avg_win is None
    assert k.avg_loss == -40.0
    assert k.profit_factor == 0.0  # gross_win 0 / gross_loss 80
    assert k.max_drawdown == -80.0


def test_compute_kpis_mixed_with_drawdown():
    """Sequence: +100, -150, +50, +200 → cum 100, -50, 0, 200; drawdown peak 100→-50 = -150."""
    df = _trades([
        {"pnl_usd": 100.0, "R_realized": 1.0,
         "closed_at": datetime(2026, 5, 1, 15, 0, tzinfo=timezone.utc)},
        {"pnl_usd": -150.0, "R_realized": -1.5,
         "closed_at": datetime(2026, 5, 2, 15, 0, tzinfo=timezone.utc)},
        {"pnl_usd": 50.0, "R_realized": 0.5,
         "closed_at": datetime(2026, 5, 3, 15, 0, tzinfo=timezone.utc)},
        {"pnl_usd": 200.0, "R_realized": 2.0,
         "closed_at": datetime(2026, 5, 4, 15, 0, tzinfo=timezone.utc)},
    ])
    k = compute_kpis(df)
    assert k.total_pnl == 200.0
    assert k.trade_count == 4
    assert k.win_rate == 0.75
    assert k.avg_win == pytest.approx((100 + 50 + 200) / 3)
    assert k.avg_loss == -150.0
    assert k.profit_factor == pytest.approx(350.0 / 150.0)
    assert k.expectancy_R == pytest.approx((1.0 - 1.5 + 0.5 + 2.0) / 4)
    assert k.max_drawdown == pytest.approx(-150.0)
    assert k.best_trade == 200.0
    assert k.worst_trade == -150.0


def test_compute_kpis_single_trade_returns_none_sharpe():
    df = _trades([{"pnl_usd": 100.0}])
    k = compute_kpis(df)
    assert k.sharpe is None  # need ≥2 daily samples
```

- [ ] **Step 2: Run the new tests, verify they pass**

Run: `pytest tests/ui/test_stats.py -v`
Expected: 5 PASS.

- [ ] **Step 3: Commit**

```bash
git add tests/ui/test_stats.py
git commit -m "test(stats): KPI coverage for win/loss/mixed/drawdown/sharpe"
```

### Task 1.3: Chart input helpers

**Files:**

- Modify: `ui/data/stats.py`
- Modify: `tests/ui/test_stats.py`

- [ ] **Step 1: Write failing tests for `equity_curve`, `daily_pnl`, `r_distribution`, `winloss_by_setup`**

Append to `tests/ui/test_stats.py`:

```python
from ui.data.stats import (
    equity_curve, daily_pnl, r_distribution, winloss_by_setup,
)


def test_equity_curve_cumulative_sorted_by_close_time():
    df = _trades([
        {"pnl_usd": 50.0,
         "closed_at": datetime(2026, 5, 2, 15, 0, tzinfo=timezone.utc)},
        {"pnl_usd": 100.0,
         "closed_at": datetime(2026, 5, 1, 15, 0, tzinfo=timezone.utc)},
        {"pnl_usd": -30.0,
         "closed_at": datetime(2026, 5, 3, 15, 0, tzinfo=timezone.utc)},
    ])
    eq = equity_curve(df)
    assert list(eq.columns) == ["closed_at", "cum_pnl"]
    assert list(eq["cum_pnl"]) == [100.0, 150.0, 120.0]


def test_daily_pnl_groups_by_calendar_day():
    df = _trades([
        {"pnl_usd": 50.0,
         "closed_at": datetime(2026, 5, 1, 14, 0, tzinfo=timezone.utc)},
        {"pnl_usd": 30.0,
         "closed_at": datetime(2026, 5, 1, 18, 0, tzinfo=timezone.utc)},
        {"pnl_usd": -20.0,
         "closed_at": datetime(2026, 5, 2, 10, 0, tzinfo=timezone.utc)},
    ])
    dp = daily_pnl(df)
    assert list(dp.columns) == ["day", "pnl"]
    assert len(dp) == 2
    assert dp.iloc[0]["pnl"] == 80.0
    assert dp.iloc[1]["pnl"] == -20.0


def test_r_distribution_bins():
    df = _trades([
        {"R_realized": 1.0}, {"R_realized": 1.5}, {"R_realized": -0.5},
        {"R_realized": -1.0}, {"R_realized": 2.5},
    ])
    series = r_distribution(df)
    assert series.tolist() == [1.0, 1.5, -0.5, -1.0, 2.5]


def test_winloss_by_setup_counts():
    df = _trades([
        {"setup_name": "vwap_bounce", "pnl_usd": 10.0},
        {"setup_name": "vwap_bounce", "pnl_usd": -5.0},
        {"setup_name": "orb",         "pnl_usd": 20.0},
        {"setup_name": "orb",         "pnl_usd": 15.0},
        {"setup_name": "orb",         "pnl_usd": -8.0},
    ])
    wl = winloss_by_setup(df)
    # columns: setup_name, wins, losses
    assert set(wl.columns) == {"setup_name", "wins", "losses"}
    rows = {r["setup_name"]: (r["wins"], r["losses"]) for _, r in wl.iterrows()}
    assert rows["vwap_bounce"] == (1, 1)
    assert rows["orb"] == (2, 1)
```

- [ ] **Step 2: Run, verify 4 failures**

Run: `pytest tests/ui/test_stats.py -v`
Expected: 4 new tests fail with `ImportError: cannot import name 'equity_curve' …`.

- [ ] **Step 3: Implement the four helpers**

Append to `ui/data/stats.py`:

```python
def equity_curve(df: pd.DataFrame) -> pd.DataFrame:
    """Cumulative PnL over time, ordered by closed_at."""
    if df.empty:
        return pd.DataFrame(columns=["closed_at", "cum_pnl"])
    s = df.sort_values("closed_at").reset_index(drop=True)
    return pd.DataFrame({
        "closed_at": s["closed_at"],
        "cum_pnl": s["pnl_usd"].astype(float).cumsum(),
    })


def daily_pnl(df: pd.DataFrame) -> pd.DataFrame:
    """Sum PnL per calendar day (UTC), ordered ascending."""
    if df.empty:
        return pd.DataFrame(columns=["day", "pnl"])
    g = (df.assign(day=pd.to_datetime(df["closed_at"]).dt.floor("D"))
            .groupby("day")["pnl_usd"].sum().astype(float)
            .reset_index().rename(columns={"pnl_usd": "pnl"}))
    return g.sort_values("day").reset_index(drop=True)


def r_distribution(df: pd.DataFrame) -> pd.Series:
    """Raw R_realized values for histogram input. Empty Series if df empty."""
    if df.empty:
        return pd.Series([], dtype=float, name="R_realized")
    return df["R_realized"].astype(float).reset_index(drop=True)


def winloss_by_setup(df: pd.DataFrame) -> pd.DataFrame:
    """Per-setup wins (pnl > 0) and losses (pnl <= 0) counts."""
    if df.empty:
        return pd.DataFrame(columns=["setup_name", "wins", "losses"])
    df2 = df.assign(
        is_win=(df["pnl_usd"].astype(float) > 0).astype(int),
        is_loss=(df["pnl_usd"].astype(float) <= 0).astype(int),
    )
    g = df2.groupby("setup_name").agg(
        wins=("is_win", "sum"),
        losses=("is_loss", "sum"),
    ).reset_index()
    return g
```

- [ ] **Step 4: Run, verify all stats tests pass**

Run: `pytest tests/ui/test_stats.py -v`
Expected: 9 PASS total.

- [ ] **Step 5: Commit**

```bash
git add ui/data/stats.py tests/ui/test_stats.py
git commit -m "feat(stats): equity curve, daily PnL, R-dist, win/loss-by-setup"
```

---

## Phase 2 — Period selector (TDD)

Pure logic that maps a preset name + a "now" instant to a `(start_utc, end_utc)` tuple.

### Task 2.1: Resolve presets to UTC ranges

**Files:**

- Create: `ui/components/__init__.py`
- Create: `ui/components/period_selector.py`
- Create: `tests/ui/test_period_selector.py`

- [ ] **Step 1: Create components package init**

```bash
touch ui/components/__init__.py
```

- [ ] **Step 2: Write failing tests for `resolve_preset`**

Create `tests/ui/test_period_selector.py`:

```python
from datetime import datetime, timezone, timedelta

import pytest

from ui.components.period_selector import resolve_preset, PRESETS


NOW = datetime(2026, 5, 28, 15, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize("preset, days", [
    ("1D", 1), ("1W", 7), ("15D", 15), ("1M", 30), ("6M", 180), ("1Y", 365),
])
def test_resolve_preset_rolling_windows(preset, days):
    start, end = resolve_preset(preset, now=NOW)
    assert end == NOW
    assert start == NOW - timedelta(days=days)


def test_presets_list_matches_spec():
    assert PRESETS == ["1D", "1W", "15D", "1M", "6M", "1Y"]


def test_resolve_preset_unknown_raises():
    with pytest.raises(ValueError):
        resolve_preset("3Y", now=NOW)
```

- [ ] **Step 3: Run, verify all 8 fail**

Run: `pytest tests/ui/test_period_selector.py -v`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 4: Implement `resolve_preset` (no Streamlit yet)**

Create `ui/components/period_selector.py`:

```python
"""Time-period selector for the dashboard.

Pure resolution logic in `resolve_preset` (unit-testable).
A separate `render` function builds the Streamlit widgets and returns
the same `(start_utc, end_utc)` tuple — kept thin so the heavy logic
stays testable.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

PRESETS = ["1D", "1W", "15D", "1M", "6M", "1Y"]

_PRESET_DAYS = {
    "1D": 1, "1W": 7, "15D": 15,
    "1M": 30, "6M": 180, "1Y": 365,
}


def resolve_preset(preset: str, *, now: Optional[datetime] = None) -> tuple[datetime, datetime]:
    """Return (start_utc, end_utc) for a preset name. `now` defaults to now-UTC."""
    if preset not in _PRESET_DAYS:
        raise ValueError(f"Unknown preset: {preset!r}. Known: {PRESETS}")
    end = now or datetime.now(timezone.utc)
    start = end - timedelta(days=_PRESET_DAYS[preset])
    return start, end
```

- [ ] **Step 5: Run, verify all 8 pass**

Run: `pytest tests/ui/test_period_selector.py -v`
Expected: 8 PASS.

- [ ] **Step 6: Commit**

```bash
git add ui/components/__init__.py ui/components/period_selector.py tests/ui/test_period_selector.py
git commit -m "feat(period-selector): preset → (start, end) UTC resolution"
```

### Task 2.2: Streamlit `render` for the selector

**Files:**

- Modify: `ui/components/period_selector.py`

- [ ] **Step 1: Append `render()` that wires presets + custom range to st.session_state**

Append to `ui/components/period_selector.py`:

```python
def render() -> tuple[datetime, datetime]:
    """Render the period selector. Returns the resolved (start, end) UTC."""
    import streamlit as st

    state = st.session_state
    state.setdefault("period_mode", "1M")  # default to 1 month
    state.setdefault("period_custom_start", (datetime.now(timezone.utc) - timedelta(days=30)).date())
    state.setdefault("period_custom_end", datetime.now(timezone.utc).date())

    cols = st.columns([6, 2, 2])
    options = PRESETS + ["Custom"]
    mode = cols[0].radio(
        "Period",
        options=options,
        index=options.index(state["period_mode"]) if state["period_mode"] in options else 0,
        horizontal=True,
        key="period_mode",
    )

    if mode == "Custom":
        start_d = cols[1].date_input("From", value=state["period_custom_start"], key="period_custom_start")
        end_d = cols[2].date_input("To", value=state["period_custom_end"], key="period_custom_end")
        start = datetime(start_d.year, start_d.month, start_d.day, 0, 0, tzinfo=timezone.utc)
        end = datetime(end_d.year, end_d.month, end_d.day, 23, 59, 59, tzinfo=timezone.utc)
        return start, end

    return resolve_preset(mode)
```

(No automated test — Streamlit rendering is exercised in the manual acceptance step. The pure logic is already tested.)

- [ ] **Step 2: Smoke-import to confirm the module still loads under pytest**

Run: `pytest tests/ui/test_period_selector.py -v`
Expected: 8 PASS (existing tests unaffected).

- [ ] **Step 3: Commit**

```bash
git add ui/components/period_selector.py
git commit -m "feat(period-selector): Streamlit render with presets + custom range"
```

---

## Phase 3 — Data layer

### Task 3.1: Shared DB engine

**Files:**

- Create: `ui/data/db.py`

- [ ] **Step 1: Implement a thin engine factory that reuses the same env vars as `state/mysql_store.py`**

Create `ui/data/db.py`:

```python
"""Shared SQLAlchemy engine for read-only dashboard queries.

Reads the same MYSQL_* env vars as state/mysql_store.py so the
dashboard container picks up the docker-compose configuration
unchanged.
"""
from __future__ import annotations

import os
from urllib.parse import quote_plus as urlquote

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

_engine: Engine | None = None


def get_engine() -> Engine:
    """Return a process-wide SQLAlchemy engine (lazily created)."""
    global _engine
    if _engine is None:
        host = os.environ.get("MYSQL_HOST", "mysql")
        port = os.environ.get("MYSQL_PORT", "3306")
        user = os.environ.get("MYSQL_USER", "trader")
        password = os.environ.get("MYSQL_PASSWORD", "traderpass")
        database = os.environ.get("MYSQL_DATABASE", "aitrader")
        url = f"mysql+pymysql://{user}:{urlquote(password)}@{host}:{port}/{database}"
        _engine = create_engine(
            url,
            pool_pre_ping=True,
            pool_recycle=3600,
            connect_args={"connect_timeout": 5},
        )
    return _engine
```

- [ ] **Step 2: Smoke-import in the test runner**

Run: `python -c "from ui.data.db import get_engine; print(get_engine())"`
Expected: prints an `Engine(...)` repr (or fails to connect, which is fine — we only verify import).

- [ ] **Step 3: Commit**

```bash
git add ui/data/db.py
git commit -m "feat(data): shared SQLAlchemy engine for dashboard reads"
```

### Task 3.2: trades_repo with integration test

**Files:**

- Create: `ui/data/trades_repo.py`
- Create: `tests/ui/test_trades_repo.py`

- [ ] **Step 1: Write the failing integration test**

Create `tests/ui/test_trades_repo.py`:

```python
"""Integration test against the real MySQL test DB.

Mirrors the project's existing pattern of testing against a live MySQL
service (per the broker-position reconciliation work). Requires
MYSQL_HOST etc to point at a reachable MySQL — in CI this is the
docker-compose `mysql` service.
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone, timedelta
from decimal import Decimal

import pandas as pd
import pytest
from sqlalchemy import text

from ui.data.db import get_engine
from ui.data.trades_repo import get_closed_trades


pytestmark = pytest.mark.skipif(
    os.environ.get("SKIP_DB_TESTS") == "1",
    reason="DB-backed test; set SKIP_DB_TESTS=1 to skip locally",
)


def _seed_strategy(name: str) -> int:
    eng = get_engine()
    with eng.begin() as conn:
        conn.execute(text("INSERT IGNORE INTO strategies (name) VALUES (:n)"), {"n": name})
        row = conn.execute(text("SELECT id FROM strategies WHERE name=:n"), {"n": name}).one()
        return row[0]


def _insert_trade(strategy_id: int, *, symbol: str, closed_at: datetime,
                   pnl: float, R: float, setup: str = "vwap_bounce") -> None:
    eng = get_engine()
    with eng.begin() as conn:
        conn.execute(text("""
            INSERT INTO trades (strategy_id, symbol, asset_class, setup_name, side, qty,
                                entry_px, exit_px, pnl_usd, R_realized, close_reason,
                                opened_at, closed_at, bars_held)
            VALUES (:sid, :sym, 'equity', :setup, 'long', 1.0,
                    100.0, :exit, :pnl, :R, 'target',
                    :opened, :closed, 5)
        """), {
            "sid": strategy_id, "sym": symbol, "setup": setup,
            "exit": 100.0 + pnl, "pnl": pnl, "R": R,
            "opened": closed_at - timedelta(hours=1), "closed": closed_at,
        })


@pytest.fixture
def isolated_strategy():
    name = f"test_{uuid.uuid4().hex[:8]}"
    sid = _seed_strategy(name)
    yield name, sid
    eng = get_engine()
    with eng.begin() as conn:
        conn.execute(text("DELETE FROM trades WHERE strategy_id=:s"), {"s": sid})
        conn.execute(text("DELETE FROM positions WHERE strategy_id=:s"), {"s": sid})
        conn.execute(text("DELETE FROM strategies WHERE id=:s"), {"s": sid})


def test_get_closed_trades_filters_by_strategy_and_window(isolated_strategy):
    name, sid = isolated_strategy
    in_window = datetime(2026, 5, 15, 14, 0, tzinfo=timezone.utc)
    out_of_window = datetime(2026, 4, 1, 14, 0, tzinfo=timezone.utc)
    _insert_trade(sid, symbol="AAPL", closed_at=in_window, pnl=10.0, R=1.0)
    _insert_trade(sid, symbol="AAPL", closed_at=out_of_window, pnl=999.0, R=10.0)

    start = datetime(2026, 5, 1, tzinfo=timezone.utc)
    end = datetime(2026, 5, 31, tzinfo=timezone.utc)
    df = get_closed_trades(name, start, end)

    assert isinstance(df, pd.DataFrame)
    assert len(df) == 1
    assert float(df.iloc[0]["pnl_usd"]) == 10.0


def test_get_closed_trades_unknown_strategy_returns_empty():
    df = get_closed_trades("does_not_exist_xyz", datetime(2026, 1, 1, tzinfo=timezone.utc),
                                                   datetime(2026, 12, 31, tzinfo=timezone.utc))
    assert df.empty
```

- [ ] **Step 2: Run, verify ImportError-style failure**

Run: `pytest tests/ui/test_trades_repo.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'ui.data.trades_repo'`.

- [ ] **Step 3: Implement the repo**

Create `ui/data/trades_repo.py`:

```python
"""Read-only access to the MySQL `trades` table for the dashboard."""
from __future__ import annotations

from datetime import datetime

import pandas as pd
from sqlalchemy import text

from ui.data.db import get_engine

_TRADE_COLS = [
    "id", "strategy_id", "symbol", "asset_class", "setup_name", "side", "qty",
    "entry_px", "exit_px", "stop_px", "target_px", "initial_stop_px",
    "pnl_usd", "R_realized", "close_reason",
    "opened_at", "closed_at", "bars_held",
]


def get_closed_trades(strategy: str, start: datetime, end: datetime) -> pd.DataFrame:
    """Return trades for `strategy` whose closed_at falls in [start, end)."""
    eng = get_engine()
    with eng.connect() as conn:
        sid_row = conn.execute(
            text("SELECT id FROM strategies WHERE name=:n"), {"n": strategy}
        ).one_or_none()
        if sid_row is None:
            return pd.DataFrame(columns=_TRADE_COLS)
        sid = sid_row[0]
        df = pd.read_sql(
            text(f"""
                SELECT {", ".join(_TRADE_COLS)}
                FROM trades
                WHERE strategy_id = :sid
                  AND closed_at >= :start
                  AND closed_at <  :end
                ORDER BY closed_at ASC
            """),
            conn,
            params={"sid": sid, "start": start, "end": end},
        )
    return df


def list_strategies() -> list[str]:
    """All strategy names known to the DB, sorted."""
    eng = get_engine()
    with eng.connect() as conn:
        rows = conn.execute(text("SELECT name FROM strategies ORDER BY name")).all()
    return [r[0] for r in rows]
```

- [ ] **Step 4: Run, verify pass**

Run: `pytest tests/ui/test_trades_repo.py -v`
Expected: 2 PASS (assuming MySQL is up via `docker compose up mysql`; otherwise set `SKIP_DB_TESTS=1`).

- [ ] **Step 5: Commit**

```bash
git add ui/data/trades_repo.py tests/ui/test_trades_repo.py
git commit -m "feat(data): trades_repo.get_closed_trades + list_strategies"
```

### Task 3.3: positions_repo with integration test

**Files:**

- Create: `ui/data/positions_repo.py`
- Create: `tests/ui/test_positions_repo.py`

- [ ] **Step 1: Write the failing integration test**

Create `tests/ui/test_positions_repo.py`:

```python
import os
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import text

from ui.data.db import get_engine
from ui.data.positions_repo import get_open


pytestmark = pytest.mark.skipif(
    os.environ.get("SKIP_DB_TESTS") == "1",
    reason="DB-backed test",
)


@pytest.fixture
def isolated_strategy():
    name = f"test_{uuid.uuid4().hex[:8]}"
    eng = get_engine()
    with eng.begin() as conn:
        conn.execute(text("INSERT INTO strategies (name) VALUES (:n)"), {"n": name})
        sid = conn.execute(text("SELECT id FROM strategies WHERE name=:n"), {"n": name}).one()[0]
    yield name, sid
    with eng.begin() as conn:
        conn.execute(text("DELETE FROM positions WHERE strategy_id=:s"), {"s": sid})
        conn.execute(text("DELETE FROM strategies WHERE id=:s"), {"s": sid})


def _insert_pos(sid, *, symbol, status, side="long"):
    eng = get_engine()
    with eng.begin() as conn:
        conn.execute(text("""
            INSERT INTO positions (strategy_id, symbol, asset_class, side, qty,
                                   entry_px, stop_px, target_px, setup_name,
                                   status, opened_at)
            VALUES (:sid, :sym, 'equity', :side, 1.0, 100.0, 99.0, 102.0,
                    'vwap_bounce', :status, :opened)
        """), {
            "sid": sid, "sym": symbol, "side": side, "status": status,
            "opened": datetime(2026, 5, 28, 14, 0, tzinfo=timezone.utc),
        })


def test_get_open_returns_only_open_positions_with_strategy_name(isolated_strategy):
    name, sid = isolated_strategy
    _insert_pos(sid, symbol="AAPL", status="open")
    _insert_pos(sid, symbol="MSFT", status="closed")
    df = get_open()
    rows = df[df["strategy"] == name]
    assert len(rows) == 1
    assert rows.iloc[0]["symbol"] == "AAPL"
    assert rows.iloc[0]["status"] == "open"
    assert {"strategy", "symbol", "side", "qty", "entry_px", "stop_px",
            "target_px", "setup_name", "asset_class", "opened_at"}.issubset(df.columns)
```

- [ ] **Step 2: Run, verify ImportError**

Run: `pytest tests/ui/test_positions_repo.py -v`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement the repo**

Create `ui/data/positions_repo.py`:

```python
"""Read-only view of open positions across all strategies."""
from __future__ import annotations

import pandas as pd
from sqlalchemy import text

from ui.data.db import get_engine


_OPEN_COLS = [
    "strategy", "symbol", "asset_class", "setup_name", "side", "qty",
    "entry_px", "stop_px", "target_px", "initial_stop_px",
    "opened_at", "status",
]


def get_open() -> pd.DataFrame:
    """All open positions across strategies, joined to the strategy name."""
    eng = get_engine()
    with eng.connect() as conn:
        df = pd.read_sql(
            text("""
                SELECT s.name AS strategy,
                       p.symbol, p.asset_class, p.setup_name, p.side, p.qty,
                       p.entry_px, p.stop_px, p.target_px, p.initial_stop_px,
                       p.opened_at, p.status
                FROM positions p
                JOIN strategies s ON s.id = p.strategy_id
                WHERE p.status = 'open'
                ORDER BY s.name, p.opened_at
            """),
            conn,
        )
    if df.empty:
        return pd.DataFrame(columns=_OPEN_COLS)
    return df
```

- [ ] **Step 4: Run, verify pass**

Run: `pytest tests/ui/test_positions_repo.py -v`
Expected: 1 PASS.

- [ ] **Step 5: Commit**

```bash
git add ui/data/positions_repo.py tests/ui/test_positions_repo.py
git commit -m "feat(data): positions_repo.get_open across strategies"
```

### Task 3.4: state_files with unit tests

**Files:**

- Create: `ui/data/state_files.py`
- Create: `tests/ui/test_state_files.py`

- [ ] **Step 1: Write failing unit tests**

Create `tests/ui/test_state_files.py`:

```python
import json
from pathlib import Path

import pytest

from ui.data.state_files import get_last_price


def _write_state(tmp_path: Path, strategy: str, payload: dict) -> Path:
    runtime = tmp_path / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    f = runtime / f"trading_state_{strategy}.json"
    f.write_text(json.dumps(payload))
    return f


def test_get_last_price_returns_value(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_state(tmp_path, "rsi_trader", {
        "symbols": [{"symbol": "AAPL", "last_price": 200.5},
                    {"symbol": "MSFT", "last_price": 410.0}],
    })
    assert get_last_price("rsi_trader", "AAPL") == 200.5
    assert get_last_price("rsi_trader", "MSFT") == 410.0


def test_get_last_price_missing_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert get_last_price("nope_strategy", "AAPL") is None


def test_get_last_price_missing_symbol(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_state(tmp_path, "rsi_trader", {"symbols": [{"symbol": "AAPL", "last_price": 200.5}]})
    assert get_last_price("rsi_trader", "TSLA") is None


def test_get_last_price_malformed_json(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "trading_state_bad.json").write_text("{not json")
    assert get_last_price("bad", "AAPL") is None


def test_get_last_price_field_absent_returns_none(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_state(tmp_path, "old_strategy", {"symbols": [{"symbol": "AAPL", "vwap": 100.0}]})
    assert get_last_price("old_strategy", "AAPL") is None
```

- [ ] **Step 2: Run, verify ImportError**

Run: `pytest tests/ui/test_state_files.py -v`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement state_files**

Create `ui/data/state_files.py`:

```python
"""Read per-strategy `runtime/trading_state_<strategy>.json` files
with safe fallbacks for the live dashboard tab.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional


def _state_path(strategy: str) -> Path:
    return Path("runtime") / f"trading_state_{strategy}.json"


def get_last_price(strategy: str, symbol: str) -> Optional[float]:
    """Return the last known price for `symbol` under `strategy`,
    or None if the file is missing/malformed or the symbol/field is absent.
    """
    path = _state_path(strategy)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    for row in data.get("symbols", []):
        if row.get("symbol") == symbol:
            v = row.get("last_price")
            return float(v) if v is not None else None
    return None
```

- [ ] **Step 4: Run, verify all 5 pass**

Run: `pytest tests/ui/test_state_files.py -v`
Expected: 5 PASS.

- [ ] **Step 5: Commit**

```bash
git add ui/data/state_files.py tests/ui/test_state_files.py
git commit -m "feat(data): state_files.get_last_price with safe fallbacks"
```

---

## Phase 4 — Theme + KPI components

### Task 4.1: Streamlit theme config

**Files:**

- Create: `.streamlit/config.toml`

- [ ] **Step 1: Create the theme file**

Create `.streamlit/config.toml`:

```toml
[theme]
base = "dark"
primaryColor = "#3b82f6"
backgroundColor = "#0b0f17"
secondaryBackgroundColor = "#141a26"
textColor = "#e5e7eb"
font = "monospace"

[server]
headless = true
```

- [ ] **Step 2: Commit**

```bash
git add .streamlit/config.toml
git commit -m "feat(theme): dark financial Streamlit theme config"
```

### Task 4.2: theme.py CSS injection + format_pnl

**Files:**

- Create: `ui/components/theme.py`
- Create: `ui/components/kpi_row.py`

- [ ] **Step 1: Implement theme injection**

Create `ui/components/theme.py`:

```python
"""Inject custom CSS for a dense, financial-platform look.

Call `inject_theme()` once at the top of dashboard.py, before any tab
content renders.
"""
from __future__ import annotations

_CSS = """
<style>
/* Tabular numerals so columns align in tables and metric tiles */
[data-testid="stMetric"], [data-testid="stMetricValue"],
[data-testid="stDataFrame"] *, .stDataFrame * {
  font-variant-numeric: tabular-nums;
}

/* Tighten metric tiles */
[data-testid="stMetric"] {
  background: #0f1422;
  border: 1px solid #1f2a3d;
  border-radius: 6px;
  padding: 10px 14px;
}
[data-testid="stMetricLabel"] { font-size: 11px; color: #9ca3af; text-transform: uppercase; letter-spacing: 0.04em; }
[data-testid="stMetricValue"] { font-size: 22px; font-weight: 600; }

/* Dense tables */
.stDataFrame [data-testid="stTable"] td,
.stDataFrame [data-testid="stTable"] th { padding: 4px 8px !important; }

/* Subtle row striping */
.stDataFrame tbody tr:nth-child(even) { background: #0f1422; }

/* Tabs more visible against dark bg */
[data-baseweb="tab-list"] { border-bottom: 1px solid #1f2a3d; }

/* PnL semantic colors used inline by format_pnl */
.pnl-pos { color: #10b981; }
.pnl-neg { color: #ef4444; }
.pnl-neu { color: #9ca3af; }
</style>
"""


def inject_theme() -> None:
    import streamlit as st
    st.markdown(_CSS, unsafe_allow_html=True)
```

- [ ] **Step 2: Implement kpi_row + format_pnl**

Create `ui/components/kpi_row.py`:

```python
"""KPI row builder + PnL formatter helpers."""
from __future__ import annotations

from typing import Optional


def format_pnl(value: Optional[float], *, prefix: str = "$") -> str:
    """Return a colored markdown string for a PnL number.

    None → '—' in neutral grey.
    Positive → emerald, Negative → red.
    """
    if value is None:
        return '<span class="pnl-neu">—</span>'
    cls = "pnl-pos" if value > 0 else ("pnl-neg" if value < 0 else "pnl-neu")
    sign = "+" if value > 0 else ""
    return f'<span class="{cls}">{sign}{prefix}{value:,.2f}</span>'


def format_pct(value: Optional[float]) -> str:
    if value is None:
        return "—"
    return f"{value * 100:.1f}%"


def render_kpi_row(kpis) -> None:
    """Render a 4×2 grid of metric tiles for a `KPIs` dataclass."""
    import streamlit as st

    row1 = st.columns(4)
    row1[0].metric("Total PnL", _money(kpis.total_pnl))
    row1[1].metric("Trades", str(kpis.trade_count))
    row1[2].metric("Win Rate", format_pct(kpis.win_rate))
    row1[3].metric("Profit Factor", _num(kpis.profit_factor, fmt="{:.2f}"))

    row2 = st.columns(4)
    row2[0].metric("Avg Win", _money(kpis.avg_win))
    row2[1].metric("Avg Loss", _money(kpis.avg_loss))
    row2[2].metric("Expectancy R", _num(kpis.expectancy_R, fmt="{:.2f}"))
    row2[3].metric("Max DD", _money(kpis.max_drawdown))

    row3 = st.columns(4)
    row3[0].metric("Sharpe", _num(kpis.sharpe, fmt="{:.2f}"))
    row3[1].metric("Avg Bars", _num(kpis.avg_bars_held, fmt="{:.1f}"))
    row3[2].metric("Best Trade", _money(kpis.best_trade))
    row3[3].metric("Worst Trade", _money(kpis.worst_trade))


def _money(v: Optional[float]) -> str:
    if v is None:
        return "—"
    sign = "-" if v < 0 else ""
    return f"{sign}${abs(v):,.2f}"


def _num(v: Optional[float], *, fmt: str) -> str:
    return "—" if v is None else fmt.format(v)
```

- [ ] **Step 3: Smoke-import**

Run: `python -c "from ui.components.theme import inject_theme; from ui.components.kpi_row import render_kpi_row, format_pnl, format_pct; print('ok')"`
Expected: prints `ok`.

- [ ] **Step 4: Commit**

```bash
git add ui/components/theme.py ui/components/kpi_row.py
git commit -m "feat(components): theme injector + KPI row + format helpers"
```

### Task 4.3: strategy_card

**Files:**

- Create: `ui/components/strategy_card.py`

- [ ] **Step 1: Implement the card**

Create `ui/components/strategy_card.py`:

```python
"""One summary card per strategy on the Strategies landing page."""
from __future__ import annotations

from ui.components.kpi_row import format_pnl, format_pct
from ui.data.stats import KPIs


def render(strategy: str, kpis: KPIs) -> bool:
    """Render the card; return True if the user clicked it (i.e. drill in)."""
    import streamlit as st

    with st.container(border=True):
        st.markdown(f"### {strategy}")
        c1, c2, c3, c4 = st.columns(4)
        c1.markdown(f"**Total PnL**<br>{format_pnl(kpis.total_pnl)}", unsafe_allow_html=True)
        c2.markdown(f"**Win Rate**<br>{format_pct(kpis.win_rate)}", unsafe_allow_html=True)
        c3.markdown(f"**# Trades**<br>{kpis.trade_count}", unsafe_allow_html=True)
        c4.markdown(f"**Max DD**<br>{format_pnl(kpis.max_drawdown)}", unsafe_allow_html=True)
        return st.button("Open detail", key=f"open_{strategy}", use_container_width=True)
```

- [ ] **Step 2: Smoke-import**

Run: `python -c "from ui.components.strategy_card import render; print('ok')"`
Expected: `ok`.

- [ ] **Step 3: Commit**

```bash
git add ui/components/strategy_card.py
git commit -m "feat(components): strategy summary card"
```

---

## Phase 5 — Tabs

### Task 5.1: Move logs_panel into ui/tabs/

**Files:**

- Create: `ui/tabs/__init__.py`
- Move: `ui/logs_panel.py` → `ui/tabs/logs_panel.py`
- Modify: `tests/test_log_reader.py` (only if it imports `ui.logs_panel`)

- [ ] **Step 1: Create the tabs package**

```bash
mkdir -p ui/tabs
touch ui/tabs/__init__.py
```

- [ ] **Step 2: git-mv the file**

```bash
git mv ui/logs_panel.py ui/tabs/logs_panel.py
```

- [ ] **Step 3: Search for any other importers and update them**

```bash
grep -rn "from ui.logs_panel\|import ui.logs_panel" --include="*.py"
```

If any are found (other than `ui/dashboard.py` which we'll rewrite anyway), update the import path to `from ui.tabs.logs_panel import …`.

- [ ] **Step 4: Run the full test suite**

Run: `pytest tests/ -x -q`
Expected: green (no behavior changed).

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "refactor: move ui/logs_panel.py into ui/tabs/"
```

### Task 5.2: live_tab

**Files:**

- Create: `ui/tabs/live_tab.py`

- [ ] **Step 1: Implement the tab**

Create `ui/tabs/live_tab.py`:

```python
"""Live Trading tab — open positions across all strategies."""
from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
import streamlit as st

from ui.data.positions_repo import get_open
from ui.data.state_files import get_last_price


def _enrich(df: pd.DataFrame) -> pd.DataFrame:
    """Add Current Px, Unrealized PnL, R-so-far, Age to a positions df."""
    if df.empty:
        return df.assign(current_px=[], unrealized=[], R_so_far=[], age=[])

    now = datetime.now(timezone.utc)
    enriched = df.copy()
    last = enriched.apply(lambda r: get_last_price(r["strategy"], r["symbol"]), axis=1)
    enriched["current_px"] = last

    def unrealized(row):
        if row["current_px"] is None:
            return None
        side_sign = 1 if row["side"] == "long" else -1
        return float((row["current_px"] - float(row["entry_px"])) * float(row["qty"]) * side_sign)

    def r_so_far(row):
        if row["current_px"] is None or row["initial_stop_px"] is None:
            return None
        risk = abs(float(row["entry_px"]) - float(row["initial_stop_px"]))
        if risk == 0:
            return None
        side_sign = 1 if row["side"] == "long" else -1
        return float((row["current_px"] - float(row["entry_px"])) * side_sign / risk)

    enriched["unrealized"] = enriched.apply(unrealized, axis=1)
    enriched["R_so_far"] = enriched.apply(r_so_far, axis=1)
    enriched["age"] = enriched["opened_at"].apply(
        lambda t: str(now - pd.Timestamp(t).to_pydatetime()).split(".")[0]
        if t is not None else "—"
    )
    return enriched


def render() -> None:
    st.subheader("Live Trading — Open Positions")

    try:
        df = get_open()
    except Exception as e:
        st.error(f"MySQL unreachable: {e}")
        st.stop()
        return

    if df.empty:
        st.info("No open positions across any strategy.")
        return

    strategies = sorted(df["strategy"].unique().tolist())
    selected = st.multiselect("Filter strategies", options=strategies, default=strategies)
    df = df[df["strategy"].isin(selected)]

    enriched = _enrich(df)

    display_cols = [
        "strategy", "symbol", "asset_class", "setup_name", "side", "qty",
        "entry_px", "current_px", "unrealized", "R_so_far",
        "stop_px", "target_px", "age",
    ]
    show = enriched[display_cols].rename(columns={
        "strategy": "Strategy", "symbol": "Symbol", "asset_class": "Asset",
        "setup_name": "Setup", "side": "Side", "qty": "Qty",
        "entry_px": "Entry", "current_px": "Current",
        "unrealized": "Unrealized PnL", "R_so_far": "R so far",
        "stop_px": "Stop", "target_px": "Target", "age": "Age",
    })
    st.dataframe(show, use_container_width=True, hide_index=True)
```

- [ ] **Step 2: Smoke-import**

Run: `python -c "from ui.tabs.live_tab import render; print('ok')"`
Expected: `ok`.

- [ ] **Step 3: Commit**

```bash
git add ui/tabs/live_tab.py
git commit -m "feat(tabs): live trading tab — open positions across strategies"
```

### Task 5.3: strategies_tab (landing + detail)

**Files:**

- Create: `ui/tabs/strategies_tab.py`

- [ ] **Step 1: Implement the tab**

Create `ui/tabs/strategies_tab.py`:

```python
"""Strategies tab — landing page (cards per strategy) + detail drill-down."""
from __future__ import annotations

from datetime import datetime

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from ui.components import period_selector, strategy_card
from ui.components.kpi_row import render_kpi_row
from ui.data import stats, trades_repo


def render() -> None:
    start, end = period_selector.render()
    st.session_state["period"] = (start, end)

    try:
        all_strategies = trades_repo.list_strategies()
    except Exception as e:
        st.error(f"MySQL unreachable: {e}")
        st.stop()
        return

    if not all_strategies:
        st.info("No strategies registered yet.")
        return

    selected: str | None = st.session_state.get("selected_strategy")

    if selected is None:
        _render_landing(all_strategies, start, end)
    else:
        if st.button("← Back to all strategies"):
            st.session_state["selected_strategy"] = None
            st.rerun()
        _render_detail(selected, start, end)


def _render_landing(strategies: list[str], start: datetime, end: datetime) -> None:
    st.subheader("Strategies")
    st.caption(f"Period: {start.date()} → {end.date()}")
    cols = st.columns(2)
    for i, name in enumerate(strategies):
        df = trades_repo.get_closed_trades(name, start, end)
        kpis = stats.compute_kpis(df)
        with cols[i % 2]:
            if strategy_card.render(name, kpis):
                st.session_state["selected_strategy"] = name
                st.rerun()


def _render_detail(strategy: str, start: datetime, end: datetime) -> None:
    st.subheader(f"Strategy — {strategy}")
    st.caption(f"Period: {start.date()} → {end.date()}")

    df = trades_repo.get_closed_trades(strategy, start, end)
    kpis = stats.compute_kpis(df)
    render_kpi_row(kpis)

    if df.empty:
        st.info("No trades in this period.")
        return

    _render_charts(df)
    _render_trades_table(df)


def _render_charts(df: pd.DataFrame) -> None:
    eq = stats.equity_curve(df)
    dp = stats.daily_pnl(df)
    rs = stats.r_distribution(df)
    wl = stats.winloss_by_setup(df)

    c1, c2 = st.columns(2)
    with c1:
        st.markdown("**Equity Curve**")
        fig = px.line(eq, x="closed_at", y="cum_pnl")
        fig.update_layout(height=300, margin=dict(l=10, r=10, t=10, b=10),
                          paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)")
        st.plotly_chart(fig, use_container_width=True)

    with c2:
        st.markdown("**Daily P&L**")
        colors = ["#10b981" if v >= 0 else "#ef4444" for v in dp["pnl"]]
        fig = go.Figure(data=[go.Bar(x=dp["day"], y=dp["pnl"], marker_color=colors)])
        fig.update_layout(height=300, margin=dict(l=10, r=10, t=10, b=10),
                          paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)")
        st.plotly_chart(fig, use_container_width=True)

    c3, c4 = st.columns(2)
    with c3:
        st.markdown("**R-Distribution**")
        fig = px.histogram(rs, nbins=20)
        fig.update_layout(height=300, margin=dict(l=10, r=10, t=10, b=10), showlegend=False,
                          paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)")
        st.plotly_chart(fig, use_container_width=True)

    with c4:
        st.markdown("**Wins / Losses by Setup**")
        if wl.empty:
            st.caption("No data.")
        else:
            fig = go.Figure(data=[
                go.Bar(name="Wins", x=wl["setup_name"], y=wl["wins"], marker_color="#10b981"),
                go.Bar(name="Losses", x=wl["setup_name"], y=wl["losses"], marker_color="#ef4444"),
            ])
            fig.update_layout(barmode="group", height=300, margin=dict(l=10, r=10, t=10, b=10),
                              paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)")
            st.plotly_chart(fig, use_container_width=True)


def _render_trades_table(df: pd.DataFrame) -> None:
    st.markdown("### Trades")
    q = st.text_input("Filter (symbol or setup)", value="").strip().lower()
    show = df.copy()
    if q:
        mask = (
            show["symbol"].str.lower().str.contains(q, na=False) |
            show["setup_name"].str.lower().str.contains(q, na=False)
        )
        show = show[mask]
    st.dataframe(show, use_container_width=True, hide_index=True)
```

- [ ] **Step 2: Smoke-import**

Run: `python -c "from ui.tabs.strategies_tab import render; print('ok')"`
Expected: `ok`.

- [ ] **Step 3: Commit**

```bash
git add ui/tabs/strategies_tab.py
git commit -m "feat(tabs): strategies tab — landing cards + detail with charts"
```

---

## Phase 6 — Dashboard entry point

### Task 6.1: Rewrite ui/dashboard.py

**Files:**

- Modify: `ui/dashboard.py` (full rewrite)

- [ ] **Step 1: Replace dashboard.py**

Replace the entire contents of `ui/dashboard.py` with:

```python
"""VWAP Wave Dashboard — entry point.

Tabs: Strategies | Live Trading | Logs | WFO
Theme: dark, financial.
Auth/TLS: handled by the nginx reverse proxy in front of this app
(see nginx/ for config). This file assumes the request reaches it
already authenticated.
"""
from __future__ import annotations

import glob
from pathlib import Path

import streamlit as st
from streamlit_autorefresh import st_autorefresh

from ui.components.theme import inject_theme
from ui.tabs import live_tab, strategies_tab
from ui.tabs.logs_panel import render as render_logs
from ui.wfo import tab as wfo_tab


st.set_page_config(page_title="aitrader", layout="wide", initial_sidebar_state="collapsed")
inject_theme()
st.title("aitrader")

strategies_t, live_t, logs_t, wfo_t = st.tabs([
    "Strategies", "Live Trading", "Logs", "WFO",
])

with strategies_t:
    strategies_tab.render()

with live_t:
    st_autorefresh(interval=5_000, key="live_refresh")
    live_tab.render()

with logs_t:
    state_files = sorted(glob.glob("runtime/trading_state_*.json"))
    strategies = [Path(f).stem.replace("trading_state_", "") for f in state_files] or ["vwap_wave"]
    selected = st.selectbox("Strategy log", strategies, key="logs_strategy")
    log_path = Path(f"logs/{selected}.log")
    if log_path.exists():
        render_logs(log_path)
    else:
        st.info(f"Log file not found at {log_path} yet.")

with wfo_t:
    wfo_tab.render()
```

- [ ] **Step 2: Smoke run**

Run: `streamlit run ui/dashboard.py --server.headless=true --server.port=8599 &
sleep 3 && curl -s http://localhost:8599/ | head -c 200 && kill %1`
Expected: an HTML response containing the page title — confirms the app boots without exceptions.

- [ ] **Step 3: Commit**

```bash
git add ui/dashboard.py
git commit -m "feat(dashboard): rewrite entry point with new tabs and dark theme"
```

---

## Phase 7 — nginx reverse proxy (HTTPS + basic auth)

### Task 7.1: nginx Dockerfile + entrypoint

**Files:**

- Create: `nginx/Dockerfile`
- Create: `nginx/entrypoint.sh`
- Create: `nginx/nginx.conf`
- Modify: `.gitignore`

- [ ] **Step 1: Create nginx files**

Create `nginx/Dockerfile`:

```dockerfile
FROM nginx:alpine

RUN apk add --no-cache apache2-utils openssl

COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Remove the default catch-all server so our conf.d/default.conf is the only one
RUN rm -f /etc/nginx/nginx.conf.default

ENTRYPOINT ["/entrypoint.sh"]
```

Create `nginx/entrypoint.sh`:

```sh
#!/bin/sh
set -e

CERT_DIR=/etc/nginx/certs
if [ ! -f "$CERT_DIR/fullchain.pem" ]; then
  mkdir -p "$CERT_DIR"
  echo "Generating self-signed cert in $CERT_DIR…"
  openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
    -keyout "$CERT_DIR/privkey.pem" \
    -out    "$CERT_DIR/fullchain.pem" \
    -subj   "/CN=aitrader-dashboard"
  chmod 600 "$CERT_DIR/privkey.pem"
fi

: "${DASH_USER:?DASH_USER is required}"
: "${DASH_PASSWORD:?DASH_PASSWORD is required}"

htpasswd -bc /etc/nginx/.htpasswd "$DASH_USER" "$DASH_PASSWORD" >/dev/null

exec nginx -g 'daemon off;'
```

Create `nginx/nginx.conf`:

```nginx
server {
    listen 80;
    server_name _;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl;
    server_name _;

    ssl_certificate     /etc/nginx/certs/fullchain.pem;
    ssl_certificate_key /etc/nginx/certs/privkey.pem;
    ssl_protocols       TLSv1.2 TLSv1.3;

    auth_basic           "aitrader";
    auth_basic_user_file /etc/nginx/.htpasswd;

    client_max_body_size 50M;

    location / {
        proxy_pass         http://dashboard:8501;
        proxy_http_version 1.1;
        proxy_set_header   Host $host;
        proxy_set_header   X-Real-IP $remote_addr;
        proxy_set_header   X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto https;

        # Streamlit needs websocket upgrade for live updates
        proxy_set_header   Upgrade $http_upgrade;
        proxy_set_header   Connection "upgrade";
        proxy_read_timeout 86400;
    }
}
```

- [ ] **Step 2: Update .gitignore so locally-generated certs aren't committed**

Append to `.gitignore`:

```
nginx/certs/
```

- [ ] **Step 3: Commit**

```bash
git add nginx/ .gitignore
git commit -m "feat(nginx): reverse proxy with self-signed TLS + HTTP basic auth"
```

### Task 7.2: docker-compose changes

**Files:**

- Modify: `docker-compose.yml`

- [ ] **Step 1: Add the nginx service and remove the dashboard host port**

Edit `docker-compose.yml`:

1. In the `dashboard` service, **remove** the `ports:` block (the lines that publish `127.0.0.1:8501:8501`). Keep everything else.
2. **Append** a new service before the `volumes:` section at the bottom of the file:

```yaml
  nginx:
    build: ./nginx
    environment:
      - DASH_USER=${DASH_USER:-admin}
      - DASH_PASSWORD=${DASH_PASSWORD:?DASH_PASSWORD must be set in .env}
    ports:
      - "127.0.0.1:80:80"
      - "127.0.0.1:443:443"
    volumes:
      - dashboard_certs:/etc/nginx/certs
    restart: unless-stopped
    depends_on:
      - dashboard
```

3. In the bottom `volumes:` block, **add** a `dashboard_certs:` entry alongside the existing `db_data:`:

```yaml
volumes:
  db_data:
  dashboard_certs:
```

- [ ] **Step 2: Validate the compose file**

Run: `docker compose config >/dev/null && echo OK`
Expected: prints `OK` (compose config parses).

If `DASH_PASSWORD must be set` appears, you need a `.env` with `DASH_PASSWORD=…` at the project root for compose to read it. Set it before running compose up:

```bash
echo "DASH_USER=admin" >> .env
echo "DASH_PASSWORD=changeme-please" >> .env
docker compose config >/dev/null && echo OK
```

(`.env` is gitignored — this is operator local config.)

- [ ] **Step 3: Commit**

```bash
git add docker-compose.yml
git commit -m "feat(compose): add nginx proxy, drop dashboard host port"
```

### Task 7.3: env.example + auth smoke test

**Files:**

- Create: `config/.env.example`
- Create: `tests/integration/test_nginx_auth.sh`

- [ ] **Step 1: Document the new env keys**

Create `config/.env.example`:

```
# Dashboard auth (used by nginx reverse proxy)
DASH_USER=admin
DASH_PASSWORD=change-me

# Trading-side env (existing, illustrative — copy from your real .env)
TRADING_ENV=production
ALPACA_API_KEY=
ALPACA_SECRET_KEY=
```

- [ ] **Step 2: Create the smoke test**

Create `tests/integration/test_nginx_auth.sh`:

```bash
#!/usr/bin/env bash
# Smoke test for the nginx reverse proxy.
# Assumes the docker-compose stack is up and DASH_USER/DASH_PASSWORD are set in .env.
set -euo pipefail

if [ ! -f .env ]; then
  echo "FAIL: .env not found at repo root" >&2
  exit 1
fi
# shellcheck disable=SC1091
. .env

assert() {
  local label="$1" expected="$2" actual="$3"
  if [ "$expected" = "$actual" ]; then
    echo "OK   $label → $actual"
  else
    echo "FAIL $label expected=$expected actual=$actual" >&2
    exit 1
  fi
}

# 1. http should redirect to https
code=$(curl -s -o /dev/null -w "%{http_code}" http://localhost/)
assert "http→https redirect" "301" "$code"

# 2. https without auth → 401
code=$(curl -k -s -o /dev/null -w "%{http_code}" https://localhost/)
assert "https no-auth"       "401" "$code"

# 3. https with bad password → 401
code=$(curl -k -s -o /dev/null -w "%{http_code}" -u "${DASH_USER}:wrong-pass" https://localhost/)
assert "https bad-password"  "401" "$code"

# 4. https with correct creds → 200
code=$(curl -k -s -o /dev/null -w "%{http_code}" -u "${DASH_USER}:${DASH_PASSWORD}" https://localhost/)
assert "https good-creds"    "200" "$code"

echo "ALL OK"
```

Make it executable:

```bash
chmod +x tests/integration/test_nginx_auth.sh
mkdir -p tests/integration
```

- [ ] **Step 3: Bring up the stack and run the smoke test**

```bash
docker compose up -d --build
# Wait for nginx to be healthy
for i in $(seq 1 30); do
  curl -k -s -o /dev/null https://localhost/ && break || sleep 1
done
./tests/integration/test_nginx_auth.sh
```

Expected: prints `OK` for all four checks and `ALL OK` at the end.

- [ ] **Step 4: Commit**

```bash
git add config/.env.example tests/integration/test_nginx_auth.sh
git commit -m "test(nginx): smoke test for HTTPS + basic auth"
```

---

## Phase 8 — Manual acceptance

### Task 8.1: Browser walkthrough

- [ ] **Step 1: Open the dashboard**

Open `https://localhost/` in a browser. Accept the self-signed cert warning. Log in with `admin` / your `DASH_PASSWORD`.

- [ ] **Step 2: Confirm dark theme**

Page background is near-black (`#0b0f17`), tabs are visible, metric tiles have a subtle border.

- [ ] **Step 3: Click through tabs**

- **Strategies** — period selector at top works (try 1D, 1M, Custom). Cards render for each running strategy. Click a card → drill-down view shows KPI row, four charts, and a trades table.
- **Live Trading** — open positions table renders. Try the strategy filter chips. Confirm `Current` and `Unrealized PnL` are populated for at least one position (relies on the Phase-0 `last_price` change being live).
- **Logs** — strategy selector + tail filter works as before.
- **WFO** — unchanged, renders.

- [ ] **Step 4: Confirm port 8501 is no longer reachable from the host**

Run: `curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8501/`
Expected: a connection refused or non-200 — Streamlit is no longer published.

- [ ] **Step 5: Stop the stack**

```bash
docker compose down
```

No commit — this task is verification only.

---

## Self-review

**Spec coverage:**

- Spec ¶1 (per-strategy stats over period) → Phase 1 (stats) + Phase 2 (period selector) + Task 5.3 (strategies tab).
- Spec ¶2 (drill into strategy) → Task 5.3 detail view + KPI row + charts + trades table.
- Spec ¶3 (live tab) → Task 5.2 + Phase 0 (`last_price`).
- Spec ¶4 (Logs kept) → Task 5.1 (move) + Task 6.1 (wire into tabs).
- Spec ¶5 (HTTPS + basic auth, single admin) → Task 7.1 + Task 7.2 + Task 7.3.
- Dark mode + financial look → Task 4.1 (theme.toml) + Task 4.2 (CSS injection + format helpers) + Task 4.3 (cards) + Task 5.3 (chart styling).
- Module layout in spec → Phases 1-5 create exactly the files listed.
- Error handling per spec → live_tab + strategies_tab catch repo exceptions; empty-period info messages; missing state file → `None` price; entrypoint.sh exits if env unset.
- Testing per spec → `tests/ui/test_stats.py`, `test_period_selector.py`, `test_state_files.py`, `test_trades_repo.py`, `test_positions_repo.py`, `tests/integration/test_nginx_auth.sh`.
- Migration / rollout → Task 7.2 removes the dashboard host port; Task 8.1 confirms.

No spec requirement is unaddressed.

**Placeholder scan:** No "TBD" / "TODO" / "implement later" / "similar to" references in any task body. Every code step shows the actual code. Every command step shows the exact command and expected output.

**Type consistency:**

- `KPIs` dataclass field names in Task 1.1 match exactly the field accesses in Task 4.2 (`render_kpi_row`) and Task 4.3 (`strategy_card.render`): `total_pnl, trade_count, win_rate, avg_win, avg_loss, profit_factor, expectancy_R, max_drawdown, sharpe, avg_bars_held, best_trade, worst_trade`.
- `equity_curve` returns columns `[closed_at, cum_pnl]` (Task 1.3); the strategies tab reads `eq["closed_at"]` and `eq["cum_pnl"]` (Task 5.3). Match.
- `daily_pnl` returns columns `[day, pnl]`; tab reads same. Match.
- `winloss_by_setup` returns `[setup_name, wins, losses]`; tab reads same. Match.
- `get_open()` returns columns including `strategy, symbol, side, qty, entry_px, stop_px, target_px, initial_stop_px, setup_name, asset_class, opened_at, status`; live_tab references all of them. Match.
- `get_last_price(strategy, symbol) -> Optional[float]` signature is identical across Task 3.4 (definition), Task 5.2 (caller).
- `resolve_preset(preset, *, now=None) -> tuple[datetime, datetime]` signature consistent in Task 2.1 and called via `period_selector.render` in Task 2.2.

No drift detected.

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-05-28-dashboard-remodel.md`. Two execution options:

**1. Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration.

**2. Inline Execution** — Execute tasks in this session using executing-plans, batch execution with checkpoints.

Which approach?
