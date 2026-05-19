# Walk-Forward Optimization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add Walk-Forward Optimization to `vwap_wave`. Tune setup parameters per `(symbol, timeframe)` over a configurable IS/OOS rolling window across the broker's tradable universe; emit a per-symbol live-config layer gated by Pardo's WFE ≥ 0.5 + positive aggregate OOS P&L.

**Architecture:** New `backtest/wfo/` package, peer to `intraday_replay.py`, orchestrating `IntradayReplay` runs over a parameter grid via `joblib`. Results stream to parquet for resumability. Aggregator emits `runtime/wfo/<run_id>/live_overrides.yaml`; `main.py` learns to layer it on top of `settings.yaml` at boot. No engine code changes.

**Tech Stack:** Python 3.11, pandas, pyarrow (parquet), joblib (process pool), python-dateutil (relativedelta for month math), pyyaml. All already pinned.

**Reference:** `docs/superpowers/specs/2026-05-19-walk-forward-optimization-design.md`

---

## Task 1: Package scaffolding

**Files:**
- Create: `backtest/wfo/__init__.py`
- Modify: `requirements.txt`

- [ ] **Step 1: Verify python-dateutil is available transitively**

Run: `.venv/bin/python -c "from dateutil.relativedelta import relativedelta; print('ok')"`
Expected: `ok`

- [ ] **Step 2: Pin python-dateutil explicitly**

Edit `requirements.txt` — append `python-dateutil==2.9.0` on a new line at the end.

- [ ] **Step 3: Create empty package init**

Write `backtest/wfo/__init__.py` with one line:

```python
"""Walk-Forward Optimization over IntradayReplay."""
```

- [ ] **Step 4: Smoke-import**

Run: `.venv/bin/python -c "import backtest.wfo; print('ok')"`
Expected: `ok`

- [ ] **Step 5: Commit**

```bash
git add backtest/wfo/__init__.py requirements.txt
git commit -m "feat(wfo): scaffold backtest/wfo package + pin python-dateutil"
```

---

## Task 2: Windowing — `parse_duration`, `Walk`, `make_walks`

**Files:**
- Create: `backtest/wfo/windowing.py`
- Test: `tests/test_wfo_windowing.py`

- [ ] **Step 1: Write the failing tests**

Write `tests/test_wfo_windowing.py`:

```python
from datetime import datetime, timedelta, timezone

import pytest
from dateutil.relativedelta import relativedelta

from backtest.wfo.windowing import Walk, make_walks, parse_duration


def test_parse_duration_days():
    assert parse_duration("180d") == timedelta(days=180)
    assert parse_duration("1d") == timedelta(days=1)


def test_parse_duration_months():
    assert parse_duration("6mo") == relativedelta(months=6)
    assert parse_duration("12mo") == relativedelta(months=12)


def test_parse_duration_invalid():
    with pytest.raises(ValueError):
        parse_duration("6w")
    with pytest.raises(ValueError):
        parse_duration("xmo")
    with pytest.raises(ValueError):
        parse_duration("")


def test_make_walks_rolling_non_overlapping():
    start = datetime(2025, 1, 1, tzinfo=timezone.utc)
    end = datetime(2025, 12, 31, tzinfo=timezone.utc)
    walks = make_walks(start, end,
                       in_sample=relativedelta(months=3),
                       out_of_sample=relativedelta(months=1))
    assert all(isinstance(w, Walk) for w in walks)
    # OOS windows must be contiguous and non-overlapping.
    for prev, nxt in zip(walks, walks[1:]):
        assert nxt.oos_start == prev.oos_end
    # Each walk's IS ends where its OOS starts.
    for w in walks:
        assert w.is_end == w.oos_start
    assert walks[0].is_start == start


def test_make_walks_drops_partial_at_end():
    start = datetime(2025, 1, 1, tzinfo=timezone.utc)
    end = datetime(2025, 4, 15, tzinfo=timezone.utc)   # not enough for full IS+OOS
    walks = make_walks(start, end,
                       in_sample=timedelta(days=90),
                       out_of_sample=timedelta(days=30))
    # Only walks whose OOS fits before `end` survive
    for w in walks:
        assert w.oos_end <= end


def test_make_walks_step_overlap_allowed():
    start = datetime(2025, 1, 1, tzinfo=timezone.utc)
    end = datetime(2025, 6, 1, tzinfo=timezone.utc)
    walks = make_walks(start, end,
                       in_sample=timedelta(days=60),
                       out_of_sample=timedelta(days=30),
                       step=timedelta(days=15))
    # Step < OOS → overlapping OOS windows
    assert walks[1].oos_start < walks[0].oos_end


def test_make_walks_anchored_not_yet_implemented():
    start = datetime(2025, 1, 1, tzinfo=timezone.utc)
    end = datetime(2025, 6, 1, tzinfo=timezone.utc)
    with pytest.raises(NotImplementedError):
        make_walks(start, end,
                   in_sample=timedelta(days=60),
                   out_of_sample=timedelta(days=30),
                   anchored=True)
```

- [ ] **Step 2: Run tests, expect failure**

Run: `.venv/bin/python -m pytest tests/test_wfo_windowing.py -v`
Expected: `ModuleNotFoundError: No module named 'backtest.wfo.windowing'` or all FAIL.

- [ ] **Step 3: Implement `windowing.py`**

Write `backtest/wfo/windowing.py`:

```python
"""IS/OOS rolling-window splitter with month/day-aware durations."""
from __future__ import annotations
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Union

from dateutil.relativedelta import relativedelta

Duration = Union[timedelta, relativedelta]

_DURATION_RE = re.compile(r"^(\d+)(d|mo)$")


def parse_duration(s: str) -> Duration:
    """Parse "6mo" → relativedelta(months=6); "180d" → timedelta(days=180)."""
    m = _DURATION_RE.match(s.strip()) if s else None
    if not m:
        raise ValueError(f"Invalid duration {s!r}; expected '<int>d' or '<int>mo'")
    n, unit = int(m.group(1)), m.group(2)
    if unit == "d":
        return timedelta(days=n)
    return relativedelta(months=n)


@dataclass(frozen=True)
class Walk:
    idx: int
    is_start: datetime
    is_end: datetime
    oos_start: datetime
    oos_end: datetime


def make_walks(start: datetime, end: datetime,
               in_sample: Duration, out_of_sample: Duration,
               step: Duration | None = None,
               anchored: bool = False) -> list[Walk]:
    """Generate rolling IS/OOS walks covering [start, end].

    Walks whose OOS would extend past `end` are dropped. `step` defaults to
    `out_of_sample` (classical Pardo: non-overlapping OOS).
    """
    if anchored:
        raise NotImplementedError("Anchored windowing reserved for v2")
    if step is None:
        step = out_of_sample

    walks: list[Walk] = []
    cursor = start
    idx = 0
    while True:
        is_start = cursor
        is_end = is_start + in_sample
        oos_start = is_end
        oos_end = oos_start + out_of_sample
        if oos_end > end:
            break
        walks.append(Walk(idx=idx, is_start=is_start, is_end=is_end,
                          oos_start=oos_start, oos_end=oos_end))
        cursor = cursor + step
        idx += 1
    return walks
```

- [ ] **Step 4: Run tests, expect green**

Run: `.venv/bin/python -m pytest tests/test_wfo_windowing.py -v`
Expected: 6 PASS.

- [ ] **Step 5: Commit**

```bash
git add backtest/wfo/windowing.py tests/test_wfo_windowing.py
git commit -m "feat(wfo): rolling IS/OOS splitter with month/day-aware durations"
```

---

## Task 3: Grid — `ParamCombo` + `expand_grid`

**Files:**
- Create: `backtest/wfo/grid.py`
- Test: `tests/test_wfo_grid.py`

- [ ] **Step 1: Write the failing tests**

Write `tests/test_wfo_grid.py`:

```python
import pytest

from backtest.wfo.grid import ParamCombo, expand_grid


def _grid_spec():
    return {
        "price_discovery": {
            "enabled": [True],
            "atr_mult_stop": [1.0, 1.5],
            "target_R": [1.5, 2.0],
            "cooldown_bars": [12],
        },
        "fade_extreme": {
            "enabled": [False],   # disabled — should produce zero combos
            "atr_mult_stop": [0.75],
            "cooldown_bars": [12],
        },
    }


def _pm_spec():
    return {
        "max_hold_bars": [12, 16],
        "breakeven_at_R": [1.0],
    }


def test_expand_grid_cardinality():
    combos = expand_grid(_grid_spec(), _pm_spec())
    # price_discovery: 1 * 2 * 2 * 1 = 4 setup combos × 2 PM combos = 8
    # fade_extreme: enabled=False → 0 combos
    assert len(combos) == 8
    assert all(c.setup == "price_discovery" for c in combos)


def test_expand_grid_setup_excluded_when_only_disabled():
    spec = {"price_discovery": {"enabled": [False],
                                "atr_mult_stop": [1.0], "cooldown_bars": [12]}}
    assert expand_grid(spec, _pm_spec()) == []


def test_expand_grid_fingerprint_stable_across_input_order():
    spec_a = _grid_spec()
    spec_b = {
        "fade_extreme": _grid_spec()["fade_extreme"],
        "price_discovery": _grid_spec()["price_discovery"],
    }
    fps_a = sorted(c.fingerprint for c in expand_grid(spec_a, _pm_spec()))
    fps_b = sorted(c.fingerprint for c in expand_grid(spec_b, _pm_spec()))
    assert fps_a == fps_b


def test_expand_grid_fingerprint_unique_per_combo():
    combos = expand_grid(_grid_spec(), _pm_spec())
    fps = [c.fingerprint for c in combos]
    assert len(set(fps)) == len(fps)


def test_param_combo_carries_setup_and_pm_values():
    combos = expand_grid(_grid_spec(), _pm_spec())
    c = combos[0]
    assert isinstance(c, ParamCombo)
    assert "atr_mult_stop" in c.setup_values
    assert "target_R" in c.setup_values
    assert "max_hold_bars" in c.pm_values
    assert "breakeven_at_R" in c.pm_values
```

- [ ] **Step 2: Run tests, expect failure**

Run: `.venv/bin/python -m pytest tests/test_wfo_grid.py -v`
Expected: ImportError or all FAIL.

- [ ] **Step 3: Implement `grid.py`**

Write `backtest/wfo/grid.py`:

```python
"""Parameter-grid expansion for WFO combos."""
from __future__ import annotations
import hashlib
import itertools
import json
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ParamCombo:
    setup: str
    setup_values: dict[str, Any]
    pm_values: dict[str, Any]
    fingerprint: str


def _fingerprint(setup: str, setup_values: dict, pm_values: dict) -> str:
    payload = json.dumps(
        {"setup": setup, "setup_values": setup_values, "pm_values": pm_values},
        sort_keys=True, separators=(",", ":"), default=str,
    )
    return hashlib.blake2b(payload.encode("utf-8"), digest_size=10).hexdigest()


def _cartesian(spec: dict[str, list[Any]]) -> list[dict[str, Any]]:
    if not spec:
        return [{}]
    keys = sorted(spec.keys())
    value_lists = [spec[k] for k in keys]
    return [dict(zip(keys, combo)) for combo in itertools.product(*value_lists)]


def expand_grid(grid_spec: dict[str, dict[str, list[Any]]],
                pm_spec: dict[str, list[Any]]) -> list[ParamCombo]:
    """Cross-multiply per-setup ranges with position-management ranges.

    A setup with `enabled: [False]` produces zero combos (disabled).
    """
    pm_combos = _cartesian(pm_spec)
    out: list[ParamCombo] = []
    for setup in sorted(grid_spec.keys()):
        spec = grid_spec[setup]
        if spec.get("enabled") == [False]:
            continue
        for setup_values in _cartesian(spec):
            for pm_values in pm_combos:
                fp = _fingerprint(setup, setup_values, pm_values)
                out.append(ParamCombo(setup=setup, setup_values=setup_values,
                                      pm_values=pm_values, fingerprint=fp))
    return out
```

- [ ] **Step 4: Run tests, expect green**

Run: `.venv/bin/python -m pytest tests/test_wfo_grid.py -v`
Expected: 5 PASS.

- [ ] **Step 5: Commit**

```bash
git add backtest/wfo/grid.py tests/test_wfo_grid.py
git commit -m "feat(wfo): parameter-grid expansion with stable fingerprints"
```

---

## Task 4: Fitness — `score`

**Files:**
- Create: `backtest/wfo/fitness.py`
- Test: `tests/test_wfo_fitness.py`

- [ ] **Step 1: Write the failing tests**

Write `tests/test_wfo_fitness.py`:

```python
import math

from backtest.wfo.fitness import score


def test_score_returns_sharpe_when_above_floor():
    metrics = {"sharpe": 1.5, "trades": 25}
    assert score(metrics, min_trades=20) == 1.5


def test_score_returns_none_when_below_floor():
    metrics = {"sharpe": 1.5, "trades": 10}
    assert score(metrics, min_trades=20) is None


def test_score_returns_none_when_sharpe_is_nan():
    metrics = {"sharpe": float("nan"), "trades": 30}
    assert score(metrics, min_trades=20) is None


def test_score_returns_zero_when_sharpe_zero():
    metrics = {"sharpe": 0.0, "trades": 30}
    assert score(metrics, min_trades=20) == 0.0


def test_score_handles_missing_trades_key():
    metrics = {"sharpe": 1.0}
    # Missing key → conservatively below floor
    assert score(metrics, min_trades=20) is None
```

- [ ] **Step 2: Run tests, expect failure**

Run: `.venv/bin/python -m pytest tests/test_wfo_fitness.py -v`
Expected: ImportError.

- [ ] **Step 3: Implement `fitness.py`**

Write `backtest/wfo/fitness.py`:

```python
"""IS-fitness scoring for parameter combos."""
from __future__ import annotations
import math


def score(metrics: dict, min_trades: int) -> float | None:
    """Return Sharpe iff trades >= min_trades and Sharpe is finite; else None."""
    if metrics.get("trades", 0) < min_trades:
        return None
    sharpe = metrics.get("sharpe", float("nan"))
    if not isinstance(sharpe, (int, float)) or math.isnan(sharpe) or math.isinf(sharpe):
        return None
    return float(sharpe)
```

- [ ] **Step 4: Run tests, expect green**

Run: `.venv/bin/python -m pytest tests/test_wfo_fitness.py -v`
Expected: 5 PASS.

- [ ] **Step 5: Commit**

```bash
git add backtest/wfo/fitness.py tests/test_wfo_fitness.py
git commit -m "feat(wfo): IS fitness — Sharpe with min_trades floor"
```

---

## Task 5: Universe — `scan_alpaca_universe` with cache

**Files:**
- Create: `backtest/wfo/universe.py`
- Test: `tests/test_wfo_universe.py`

- [ ] **Step 1: Write the failing tests**

Write `tests/test_wfo_universe.py`:

```python
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from backtest.wfo.universe import scan_alpaca_universe


def _asset(symbol, asset_class="us_equity", *, status="active", tradable=True):
    return {"symbol": symbol, "class": asset_class, "status": status, "tradable": tradable}


def _bars_with_volume(symbol, *, dollar_volume):
    base = datetime(2026, 5, 1, tzinfo=timezone.utc)
    px = 100.0
    qty = dollar_volume / px
    return [
        {"t": (base + timedelta(days=i)).isoformat().replace("+00:00", "Z"),
         "o": px, "h": px, "l": px, "c": px, "v": qty}
        for i in range(20)
    ]


def _client(asset_list, dollar_volumes):
    """dollar_volumes: dict[symbol] -> float."""
    client = MagicMock()
    client.get_assets.return_value = asset_list
    client.get_stock_bars.side_effect = lambda symbol, tf, start, end: \
        _bars_with_volume(symbol, dollar_volume=dollar_volumes[symbol])
    client.get_crypto_bars.side_effect = lambda symbol, tf, start, end: \
        _bars_with_volume(symbol, dollar_volume=dollar_volumes[symbol])
    return client


def test_scan_filters_inactive_and_untradable(tmp_path):
    client = _client(
        [_asset("AAPL"), _asset("FOO", status="inactive"),
         _asset("BAR", tradable=False)],
        {"AAPL": 50_000_000},
    )
    out = scan_alpaca_universe(client, classes=["us_equity"],
                               min_dollar_volume_20d=1_000_000,
                               top_n_per_class={"us_equity": 10},
                               cache_dir=tmp_path,
                               asof_date=date(2026, 5, 19))
    assert out == [("AAPL", "us_equity")]


def test_scan_drops_below_volume_floor(tmp_path):
    client = _client(
        [_asset("AAPL"), _asset("MEH"), _asset("PENNY")],
        {"AAPL": 50_000_000, "MEH": 8_000_000, "PENNY": 100_000},
    )
    out = scan_alpaca_universe(client, classes=["us_equity"],
                               min_dollar_volume_20d=5_000_000,
                               top_n_per_class={"us_equity": 10},
                               cache_dir=tmp_path,
                               asof_date=date(2026, 5, 19))
    assert sorted(out) == [("AAPL", "us_equity"), ("MEH", "us_equity")]


def test_scan_top_n_caps_per_class(tmp_path):
    client = _client(
        [_asset("AAA"), _asset("BBB"), _asset("CCC"), _asset("DDD")],
        {"AAA": 90_000_000, "BBB": 80_000_000,
         "CCC": 70_000_000, "DDD": 60_000_000},
    )
    out = scan_alpaca_universe(client, classes=["us_equity"],
                               min_dollar_volume_20d=1_000_000,
                               top_n_per_class={"us_equity": 2},
                               cache_dir=tmp_path,
                               asof_date=date(2026, 5, 19))
    # Sort-by-liquidity-desc → AAA, BBB win
    assert out == [("AAA", "us_equity"), ("BBB", "us_equity")]


def test_scan_top_n_none_means_no_cap(tmp_path):
    client = _client(
        [_asset("X"), _asset("Y"), _asset("Z")],
        {"X": 90_000_000, "Y": 80_000_000, "Z": 70_000_000},
    )
    out = scan_alpaca_universe(client, classes=["us_equity"],
                               min_dollar_volume_20d=1_000_000,
                               top_n_per_class={"us_equity": None},
                               cache_dir=tmp_path,
                               asof_date=date(2026, 5, 19))
    assert len(out) == 3


def test_scan_uses_cache_on_repeat(tmp_path):
    client = _client([_asset("AAPL")], {"AAPL": 50_000_000})
    kwargs = dict(classes=["us_equity"], min_dollar_volume_20d=1_000_000,
                  top_n_per_class={"us_equity": 10}, cache_dir=tmp_path,
                  asof_date=date(2026, 5, 19))
    scan_alpaca_universe(client, **kwargs)
    first_calls = client.get_assets.call_count + client.get_stock_bars.call_count
    scan_alpaca_universe(client, **kwargs)
    second_calls = client.get_assets.call_count + client.get_stock_bars.call_count
    # Second call should not have hit the broker
    assert second_calls == first_calls
```

- [ ] **Step 2: Run tests, expect failure**

Run: `.venv/bin/python -m pytest tests/test_wfo_universe.py -v`
Expected: ImportError.

- [ ] **Step 3: Implement `universe.py`**

Write `backtest/wfo/universe.py`:

```python
"""Broker-asset scan with liquidity floor and disk-cached results."""
from __future__ import annotations
import hashlib
import json
import logging
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Iterable

import pandas as pd

logger = logging.getLogger(__name__)

_BARS_LOOKBACK_DAYS = 30          # fetch 30 calendar days, take last 20 trading bars


def _cache_key(asof_date: date, classes: list[str], floor: float,
               top_n: dict[str, int | None]) -> str:
    payload = json.dumps({"asof": asof_date.isoformat(),
                          "classes": sorted(classes),
                          "floor": floor,
                          "top_n": dict(sorted(top_n.items()))},
                         sort_keys=True, separators=(",", ":"))
    return hashlib.blake2b(payload.encode("utf-8"), digest_size=8).hexdigest()


def _read_cache(cache_path: Path) -> list[tuple[str, str]] | None:
    if not cache_path.exists():
        return None
    df = pd.read_parquet(cache_path)
    return list(zip(df["symbol"].tolist(), df["asset_class"].tolist()))


def _write_cache(cache_path: Path, rows: list[tuple[str, str]]) -> None:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame(rows, columns=["symbol", "asset_class"])
    tmp = cache_path.with_suffix(cache_path.suffix + ".tmp")
    df.to_parquet(tmp, index=False)
    tmp.replace(cache_path)


def _dollar_volume_20d(client, symbol: str, asset_class: str,
                       end: datetime) -> float:
    start = end - timedelta(days=_BARS_LOOKBACK_DAYS)
    if asset_class == "crypto":
        bars = client.get_crypto_bars(symbol, "1Day", start, end)
    else:
        bars = client.get_stock_bars(symbol, "1Day", start, end)
    if not bars:
        return 0.0
    last = bars[-20:]
    return float(sum(b["c"] * b["v"] for b in last) / max(len(last), 1))


def scan_alpaca_universe(
    client,
    *,
    classes: list[str],
    min_dollar_volume_20d: float,
    top_n_per_class: dict[str, int | None],
    cache_dir: Path | str,
    asof_date: date | None = None,
) -> list[tuple[str, str]]:
    """Return [(symbol, asset_class)] for active+tradable+liquid+top-N assets.

    Cached on disk per (asof_date, classes, floor, top_n) tuple.
    """
    asof_date = asof_date or date.today()
    cache_dir = Path(cache_dir)
    key = _cache_key(asof_date, classes, min_dollar_volume_20d, top_n_per_class)
    cache_path = cache_dir / f"{asof_date.isoformat()}_{key}.parquet"

    cached = _read_cache(cache_path)
    if cached is not None:
        return cached

    end_dt = datetime.combine(asof_date, time(0, tzinfo=timezone.utc))
    by_class: dict[str, list[tuple[str, float]]] = {c: [] for c in classes}

    assets = client.get_assets()
    for a in assets:
        if a.get("class") not in classes:
            continue
        if a.get("status") != "active" or not a.get("tradable"):
            continue
        symbol = a["symbol"]
        try:
            dv = _dollar_volume_20d(client, symbol, a["class"], end_dt)
        except Exception as exc:                                # noqa: BLE001
            logger.warning("UNIVERSE_BARS_FAILED symbol=%s err=%s", symbol, exc)
            continue
        if dv < min_dollar_volume_20d:
            continue
        by_class[a["class"]].append((symbol, dv))

    out: list[tuple[str, str]] = []
    for cls in classes:
        rows = sorted(by_class[cls], key=lambda r: r[1], reverse=True)
        cap = top_n_per_class.get(cls)
        if cap is not None:
            rows = rows[:cap]
        out.extend((sym, cls) for sym, _ in rows)

    _write_cache(cache_path, out)
    return out
```

- [ ] **Step 4: Run tests, expect green**

Run: `.venv/bin/python -m pytest tests/test_wfo_universe.py -v`
Expected: 5 PASS.

- [ ] **Step 5: Commit**

```bash
git add backtest/wfo/universe.py tests/test_wfo_universe.py
git commit -m "feat(wfo): broker-universe scan with liquidity floor + parquet cache"
```

---

## Task 6: Runner — `_run_one` (per-task function)

**Files:**
- Create: `backtest/wfo/runner.py` (skeleton + `_run_one` only)
- Test: `tests/test_wfo_runner.py` (per-task cases only)

- [ ] **Step 1: Write the failing tests**

Write `tests/test_wfo_runner.py`:

```python
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from backtest.wfo.grid import ParamCombo
from backtest.wfo.runner import RUNNER_RESULT_COLUMNS, RunTask, _run_one
from backtest.wfo.windowing import Walk
from core.asset_class import AssetClassConfig
from core.bar import Bar


CRYPTO = AssetClassConfig(
    name="crypto", timezone="UTC",
    session_open_local="00:00", session_close_local="23:59",
    opening_blackout_min=0, bar_timeframe="5Min",
    slippage_bps=0.0, commission_per_share=0.0, commission_bps=0.0,
)


def _flat_bars(symbol, n, base=None, c=100.0):
    base = base or datetime(2026, 1, 1, tzinfo=timezone.utc)
    return [Bar(symbol=symbol, ts=base + timedelta(minutes=5 * i),
                open=c, high=c, low=c, close=c, volume=100) for i in range(n)]


def _walk():
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return Walk(idx=0,
                is_start=base, is_end=base + timedelta(days=1),
                oos_start=base + timedelta(days=1),
                oos_end=base + timedelta(days=2))


def _combo():
    return ParamCombo(
        setup="price_discovery",
        setup_values={"enabled": True, "atr_mult_stop": 1.0, "target_R": 1.5,
                      "arm_window_bars": 6, "cooldown_bars": 12},
        pm_values={"max_hold_bars": 12, "breakeven_at_R": 1.0},
        fingerprint="abc123",
    )


def _task(*, is_bars, oos_bars, combo=None):
    return RunTask(
        symbol="BTC/USD", asset_class="crypto", timeframe="5Min",
        walk=_walk(),
        is_bars=is_bars, oos_bars=oos_bars,
        combo=combo or _combo(),
        initial_equity=100_000.0,
        ac_configs={"crypto": CRYPTO},
        risk_cfg={
            "max_risk_per_trade": 0.005, "max_notional_per_trade_pct": 0.20,
            "max_concurrent_positions": 4, "max_daily_risk_open": 0.02,
            "consecutive_loss_limit": 2, "loss_filter_scope": "per_symbol",
            "circuit_breaker": {"daily_loss_limit_1": 0.02,
                                "daily_loss_limit_2": 0.03,
                                "drawdown_limit": 0.10},
        },
        filters_cfg={"opening_blackout_min": 0, "volume_deficit_pct": 0.30},
        min_trades=1,
    )


def test_run_one_returns_full_schema():
    row = _run_one(_task(is_bars=_flat_bars("BTC/USD", 50),
                         oos_bars=_flat_bars("BTC/USD", 50)))
    assert isinstance(row, dict)
    assert set(row.keys()) >= set(RUNNER_RESULT_COLUMNS)
    assert row["symbol"] == "BTC/USD"
    assert row["asset_class"] == "crypto"
    assert row["timeframe"] == "5Min"
    assert row["walk_idx"] == 0
    assert row["fingerprint"] == "abc123"


def test_run_one_below_min_trades_status():
    # Flat bars produce zero trades; min_trades=20 forces below-floor
    task = _task(is_bars=_flat_bars("BTC/USD", 50),
                 oos_bars=_flat_bars("BTC/USD", 50))
    task.min_trades = 20
    row = _run_one(task)
    assert row["status"] == "below_min_trades"
    assert pd.isna(row["is_score"])
    assert pd.isna(row["oos_sharpe"])


def test_run_one_failed_status_on_exception(monkeypatch):
    """Force IntradayReplay to raise; verify failed-status row, no propagation."""
    import backtest.wfo.runner as runner_mod

    def boom(*args, **kwargs):
        raise RuntimeError("synthetic failure")

    monkeypatch.setattr(runner_mod, "IntradayReplay", boom)
    row = _run_one(_task(is_bars=_flat_bars("BTC/USD", 50),
                         oos_bars=_flat_bars("BTC/USD", 50)))
    assert row["status"] == "failed"
    assert "synthetic failure" in row["error"]
    assert pd.isna(row["is_sharpe"])


def test_run_one_idempotent_for_same_input():
    task = _task(is_bars=_flat_bars("BTC/USD", 50),
                 oos_bars=_flat_bars("BTC/USD", 50))
    row_a = _run_one(task)
    row_b = _run_one(task)
    # Stable across runs (Phase-8 determinism gate)
    assert row_a == row_b
```

- [ ] **Step 2: Run tests, expect failure**

Run: `.venv/bin/python -m pytest tests/test_wfo_runner.py -v`
Expected: ImportError.

- [ ] **Step 3: Implement `runner.py` skeleton + `_run_one`**

Write `backtest/wfo/runner.py`:

```python
"""WFO orchestrator — outer-loop runner and per-task `_run_one`."""
from __future__ import annotations
import logging
import math
from copy import deepcopy
from dataclasses import dataclass, field

from backtest.fill_engine import SimulatedFillEngine     # noqa: F401  (used by IntradayReplay)
from backtest.intraday_replay import IntradayReplay
from backtest.performance import compute_metrics
from backtest.wfo.fitness import score
from backtest.wfo.grid import ParamCombo
from backtest.wfo.windowing import Walk
from core.asset_class import AssetClassConfig
from core.bar import Bar

logger = logging.getLogger(__name__)


RUNNER_RESULT_COLUMNS = (
    "symbol", "asset_class", "timeframe", "walk_idx",
    "setup", "fingerprint", "combo_values_json",
    "is_sharpe", "is_trades", "is_pnl", "is_score",
    "oos_sharpe", "oos_trades", "oos_pnl", "oos_max_dd", "oos_avg_R",
    "status", "error",
)


@dataclass
class RunTask:
    symbol: str
    asset_class: str
    timeframe: str
    walk: Walk
    is_bars: list[Bar]
    oos_bars: list[Bar]
    combo: ParamCombo
    initial_equity: float
    ac_configs: dict[str, AssetClassConfig]
    risk_cfg: dict
    filters_cfg: dict
    min_trades: int


def _build_replay_cfg(task: RunTask) -> dict:
    """Synthesize a one-symbol IntradayReplay config from the task.

    All setups except the combo's are forced disabled; the combo's setup
    receives its setup_values; position_management receives pm_values.
    """
    setups: dict = {}
    for name in ("price_discovery", "fade_extreme", "return_to_value", "vwap_bounce"):
        if name == task.combo.setup:
            base = {"enabled": True, "atr_mult_stop": 1.0, "cooldown_bars": 12}
            base.update(task.combo.setup_values)
            # Carry through commonly-required keys that some setups read with .get()
            base.setdefault("target_R", 1.5)
            base.setdefault("arm_window_bars", 6)
            base.setdefault("scale_offsets_atr", [0.0, 0.25, 0.5])
            base.setdefault("scale_weights", [0.4, 0.35, 0.25])
            setups[name] = base
        else:
            setups[name] = {
                "enabled": False, "atr_mult_stop": 1.0, "target_R": 1.5,
                "arm_window_bars": 6, "cooldown_bars": 12,
                "scale_offsets_atr": [0.0], "scale_weights": [1.0],
            }
    return {
        "setups": setups,
        "risk": deepcopy(task.risk_cfg),
        "filters": deepcopy(task.filters_cfg),
        "position_management": deepcopy(task.combo.pm_values),
        "scheduler": {"bar_timeframe": task.timeframe},
    }


def _empty_metric_row(task: RunTask, *, status: str, error: str = "") -> dict:
    import json
    return {
        "symbol": task.symbol, "asset_class": task.asset_class,
        "timeframe": task.timeframe, "walk_idx": task.walk.idx,
        "setup": task.combo.setup, "fingerprint": task.combo.fingerprint,
        "combo_values_json": json.dumps({"setup": task.combo.setup_values,
                                         "pm": task.combo.pm_values},
                                        sort_keys=True),
        "is_sharpe": math.nan, "is_trades": 0, "is_pnl": 0.0,
        "is_score": math.nan,
        "oos_sharpe": math.nan, "oos_trades": 0, "oos_pnl": 0.0,
        "oos_max_dd": math.nan, "oos_avg_R": math.nan,
        "status": status, "error": error,
    }


def _run_one(task: RunTask) -> dict:
    """Run one (symbol, timeframe, walk, combo). Never raises."""
    try:
        cfg = _build_replay_cfg(task)
        is_result = IntradayReplay(
            symbols=[(task.symbol, task.asset_class)],
            asset_class_configs=task.ac_configs,
            bars={task.symbol: task.is_bars},
            initial_equity=task.initial_equity,
            config=cfg,
        ).run()
        is_metrics = compute_metrics(is_result.equity_curve, is_result.trades)
        is_score = score(is_metrics, min_trades=task.min_trades)

        if is_score is None:
            row = _empty_metric_row(task, status="below_min_trades")
            row["is_sharpe"] = is_metrics.get("sharpe", math.nan)
            row["is_trades"] = int(is_metrics.get("trades", 0))
            row["is_pnl"] = float(is_result.trades["pnl_usd"].sum()
                                  if not is_result.trades.empty else 0.0)
            return row

        oos_result = IntradayReplay(
            symbols=[(task.symbol, task.asset_class)],
            asset_class_configs=task.ac_configs,
            bars={task.symbol: task.oos_bars},
            initial_equity=task.initial_equity,
            config=cfg,
        ).run()
        oos_metrics = compute_metrics(oos_result.equity_curve, oos_result.trades)

        row = _empty_metric_row(task, status="ok")
        row["is_sharpe"] = is_metrics.get("sharpe", math.nan)
        row["is_trades"] = int(is_metrics.get("trades", 0))
        row["is_pnl"] = float(is_result.trades["pnl_usd"].sum()
                              if not is_result.trades.empty else 0.0)
        row["is_score"] = is_score
        row["oos_sharpe"] = oos_metrics.get("sharpe", math.nan)
        row["oos_trades"] = int(oos_metrics.get("trades", 0))
        row["oos_pnl"] = float(oos_result.trades["pnl_usd"].sum()
                               if not oos_result.trades.empty else 0.0)
        row["oos_max_dd"] = oos_metrics.get("max_drawdown", math.nan)
        row["oos_avg_R"] = oos_metrics.get("avg_R", math.nan)
        return row
    except Exception as exc:                                    # noqa: BLE001
        return _empty_metric_row(task, status="failed", error=repr(exc))
```

- [ ] **Step 4: Run tests, expect green**

Run: `.venv/bin/python -m pytest tests/test_wfo_runner.py -v`
Expected: 4 PASS.

- [ ] **Step 5: Commit**

```bash
git add backtest/wfo/runner.py tests/test_wfo_runner.py
git commit -m "feat(wfo): _run_one per-task — IS/OOS replay + status row"
```

---

## Task 7: Runner — `WFORunner` orchestration + parquet streaming

**Files:**
- Modify: `backtest/wfo/runner.py` (append `WFORunner`)
- Modify: `tests/test_wfo_runner.py` (append orchestration tests)

- [ ] **Step 1: Append failing orchestration tests**

Append to `tests/test_wfo_runner.py`:

```python
from pathlib import Path

from backtest.wfo.runner import WFORunner


def _runner_cfg(*, history_start, history_end):
    return {
        "run": {"parallelism": 1, "random_seed": 42, "output_root": "runtime/wfo"},
        "history": {"start": history_start, "end": history_end,
                    "initial_equity": 100_000.0},
        "windowing": {"in_sample": "1d", "out_of_sample": "1d", "step": None},
        "timeframes": ["5Min"],
        "fitness": {"metric": "sharpe", "min_trades": 1},
        "gate": {"wfe_min": 0.5, "require_positive_oos_pnl": True},
        "grid": {
            "price_discovery": {
                "enabled": [True],
                "atr_mult_stop": [1.0, 1.5],
                "target_R": [1.5],
                "arm_window_bars": [6],
                "cooldown_bars": [12],
            },
        },
        "position_management": {"max_hold_bars": [12], "breakeven_at_R": [1.0]},
        "risk": {
            "max_risk_per_trade": 0.005, "max_notional_per_trade_pct": 0.20,
            "max_concurrent_positions": 4, "max_daily_risk_open": 0.02,
            "consecutive_loss_limit": 2, "loss_filter_scope": "per_symbol",
            "circuit_breaker": {"daily_loss_limit_1": 0.02,
                                "daily_loss_limit_2": 0.03,
                                "drawdown_limit": 0.10},
        },
        "filters": {"opening_blackout_min": 0, "volume_deficit_pct": 0.30},
    }


def test_runner_smoke_writes_results_parquet(tmp_path):
    bars = {"BTC/USD": _flat_bars("BTC/USD", n=300,
                                   base=datetime(2026, 1, 1, tzinfo=timezone.utc))}
    runner = WFORunner(
        cfg=_runner_cfg(history_start=datetime(2026, 1, 1, tzinfo=timezone.utc),
                        history_end=datetime(2026, 1, 4, tzinfo=timezone.utc)),
        asset_class_configs={"crypto": CRYPTO},
        symbols=[("BTC/USD", "crypto")],
        bars_loader=lambda sym, ac, tf: bars[sym],
        output_dir=tmp_path,
    )
    parquet_path = runner.run()
    assert parquet_path.exists()
    df = pd.read_parquet(parquet_path)
    # 2 walks × 2 combos = 4 rows
    assert len(df) == 4
    assert set(df["fingerprint"].unique()).__len__() == 2


def test_runner_skips_pair_with_empty_bars(tmp_path, caplog):
    runner = WFORunner(
        cfg=_runner_cfg(history_start=datetime(2026, 1, 1, tzinfo=timezone.utc),
                        history_end=datetime(2026, 1, 4, tzinfo=timezone.utc)),
        asset_class_configs={"crypto": CRYPTO},
        symbols=[("BTC/USD", "crypto"), ("ETH/USD", "crypto")],
        bars_loader=lambda sym, ac, tf: (
            _flat_bars(sym, 300, base=datetime(2026, 1, 1, tzinfo=timezone.utc))
            if sym == "BTC/USD" else []
        ),
        output_dir=tmp_path,
    )
    with caplog.at_level("WARNING"):
        runner.run()
    df = pd.read_parquet(tmp_path / "results.parquet")
    assert set(df["symbol"].unique()) == {"BTC/USD"}
    assert any("BARS_UNAVAILABLE" in r.message for r in caplog.records)
```

- [ ] **Step 2: Run tests, expect failure**

Run: `.venv/bin/python -m pytest tests/test_wfo_runner.py::test_runner_smoke_writes_results_parquet -v`
Expected: ImportError on `WFORunner`.

- [ ] **Step 3: Append `WFORunner` to `runner.py`**

Append to `backtest/wfo/runner.py`:

```python
from datetime import datetime
from pathlib import Path
from typing import Callable

import pyarrow as pa
import pyarrow.parquet as pq
from joblib import Parallel, delayed

from backtest.wfo.grid import expand_grid
from backtest.wfo.windowing import Walk, make_walks, parse_duration


_PARQUET_SCHEMA = pa.schema([
    pa.field("symbol", pa.string()),
    pa.field("asset_class", pa.string()),
    pa.field("timeframe", pa.string()),
    pa.field("walk_idx", pa.int32()),
    pa.field("setup", pa.string()),
    pa.field("fingerprint", pa.string()),
    pa.field("combo_values_json", pa.string()),
    pa.field("is_sharpe", pa.float64()),
    pa.field("is_trades", pa.int32()),
    pa.field("is_pnl", pa.float64()),
    pa.field("is_score", pa.float64()),
    pa.field("oos_sharpe", pa.float64()),
    pa.field("oos_trades", pa.int32()),
    pa.field("oos_pnl", pa.float64()),
    pa.field("oos_max_dd", pa.float64()),
    pa.field("oos_avg_R", pa.float64()),
    pa.field("status", pa.string()),
    pa.field("error", pa.string()),
])


def _slice_bars(bars: list[Bar], start: datetime, end: datetime) -> list[Bar]:
    return [b for b in bars if start <= b.ts < end]


@dataclass
class WFORunner:
    cfg: dict
    asset_class_configs: dict[str, AssetClassConfig]
    symbols: list[tuple[str, str]]
    bars_loader: Callable[[str, str, str], list[Bar]]
    output_dir: Path

    def run(self) -> Path:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        parquet_path = self.output_dir / "results.parquet"

        history = self.cfg["history"]
        start = self._parse_dt(history["start"])
        end = self._parse_dt(history["end"])
        is_dur = parse_duration(self.cfg["windowing"]["in_sample"])
        oos_dur = parse_duration(self.cfg["windowing"]["out_of_sample"])
        step_str = self.cfg["windowing"].get("step")
        step = parse_duration(step_str) if step_str else None
        walks = make_walks(start, end, in_sample=is_dur, out_of_sample=oos_dur, step=step)

        combos = expand_grid(self.cfg["grid"], self.cfg["position_management"])
        timeframes = self.cfg["timeframes"]
        n_jobs = self.cfg["run"]["parallelism"]

        writer = pq.ParquetWriter(parquet_path, _PARQUET_SCHEMA)
        try:
            for symbol, asset_class in self.symbols:
                for timeframe in timeframes:
                    bars = self.bars_loader(symbol, asset_class, timeframe)
                    if not bars:
                        logger.warning("BARS_UNAVAILABLE symbol=%s tf=%s",
                                       symbol, timeframe)
                        continue
                    tasks = self._build_tasks(symbol, asset_class, timeframe,
                                              bars, walks, combos)
                    if not tasks:
                        continue
                    rows = Parallel(n_jobs=n_jobs, backend="loky")(
                        delayed(_run_one)(t) for t in tasks
                    )
                    self._write_rows(writer, rows)
        finally:
            writer.close()
        return parquet_path

    @staticmethod
    def _parse_dt(value) -> datetime:
        if isinstance(value, datetime):
            return value
        from datetime import timezone
        return datetime.fromisoformat(str(value)).replace(tzinfo=timezone.utc)

    def _build_tasks(self, symbol, asset_class, timeframe, bars, walks, combos):
        out: list[RunTask] = []
        for walk in walks:
            is_bars = _slice_bars(bars, walk.is_start, walk.is_end)
            oos_bars = _slice_bars(bars, walk.oos_start, walk.oos_end)
            if not is_bars or not oos_bars:
                continue
            for combo in combos:
                out.append(RunTask(
                    symbol=symbol, asset_class=asset_class, timeframe=timeframe,
                    walk=walk, is_bars=is_bars, oos_bars=oos_bars, combo=combo,
                    initial_equity=self.cfg["history"]["initial_equity"],
                    ac_configs=self.asset_class_configs,
                    risk_cfg=self.cfg["risk"],
                    filters_cfg=self.cfg["filters"],
                    min_trades=self.cfg["fitness"]["min_trades"],
                ))
        return out

    @staticmethod
    def _write_rows(writer: pq.ParquetWriter, rows: list[dict]) -> None:
        if not rows:
            return
        table = pa.Table.from_pylist(rows, schema=_PARQUET_SCHEMA)
        writer.write_table(table)
```

- [ ] **Step 4: Run tests, expect green**

Run: `.venv/bin/python -m pytest tests/test_wfo_runner.py -v`
Expected: 6 PASS (4 prior + 2 new).

- [ ] **Step 5: Commit**

```bash
git add backtest/wfo/runner.py tests/test_wfo_runner.py
git commit -m "feat(wfo): WFORunner — joblib over (walk × combo), parquet streaming"
```

---

## Task 8: Runner — resumability

**Files:**
- Modify: `backtest/wfo/runner.py` (resume-aware task filtering)
- Modify: `tests/test_wfo_runner.py` (append resume test)

- [ ] **Step 1: Append failing resume test**

Append to `tests/test_wfo_runner.py`:

```python
def test_runner_resume_skips_completed_tasks(tmp_path):
    bars = {"BTC/USD": _flat_bars("BTC/USD", n=300,
                                   base=datetime(2026, 1, 1, tzinfo=timezone.utc))}
    cfg = _runner_cfg(history_start=datetime(2026, 1, 1, tzinfo=timezone.utc),
                      history_end=datetime(2026, 1, 4, tzinfo=timezone.utc))
    runner = WFORunner(
        cfg=cfg, asset_class_configs={"crypto": CRYPTO},
        symbols=[("BTC/USD", "crypto")],
        bars_loader=lambda sym, ac, tf: bars[sym],
        output_dir=tmp_path,
    )
    runner.run()
    df_first = pd.read_parquet(tmp_path / "results.parquet")
    n_first = len(df_first)

    # Re-run: every task already in parquet → no new rows
    runner.run()
    df_second = pd.read_parquet(tmp_path / "results.parquet")
    assert len(df_second) == n_first
    # No duplicate (symbol, timeframe, walk_idx, fingerprint) keys
    keys = list(zip(df_second["symbol"], df_second["timeframe"],
                    df_second["walk_idx"], df_second["fingerprint"]))
    assert len(keys) == len(set(keys))
```

- [ ] **Step 2: Run test, expect failure**

Run: `.venv/bin/python -m pytest tests/test_wfo_runner.py::test_runner_resume_skips_completed_tasks -v`
Expected: row count doubles on second run; assertion fails.

- [ ] **Step 3: Add resume filtering**

Modify `backtest/wfo/runner.py`. In the `WFORunner.run` method, before the writer is opened, load existing keys; pass them to `_build_tasks` for filtering.

Replace `WFORunner.run` and `WFORunner._build_tasks` with:

```python
    def run(self) -> Path:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        parquet_path = self.output_dir / "results.parquet"

        history = self.cfg["history"]
        start = self._parse_dt(history["start"])
        end = self._parse_dt(history["end"])
        is_dur = parse_duration(self.cfg["windowing"]["in_sample"])
        oos_dur = parse_duration(self.cfg["windowing"]["out_of_sample"])
        step_str = self.cfg["windowing"].get("step")
        step = parse_duration(step_str) if step_str else None
        walks = make_walks(start, end, in_sample=is_dur, out_of_sample=oos_dur, step=step)

        combos = expand_grid(self.cfg["grid"], self.cfg["position_management"])
        timeframes = self.cfg["timeframes"]
        n_jobs = self.cfg["run"]["parallelism"]

        completed = self._load_completed_keys(parquet_path)
        # Open writer in append mode by reading existing rows once and
        # rewriting them as the writer's first batch (ParquetWriter cannot
        # itself open in append mode; this preserves the durability invariant).
        writer = pq.ParquetWriter(parquet_path.with_suffix(".parquet.tmp"),
                                  _PARQUET_SCHEMA)
        try:
            if completed:
                self._copy_existing_rows(parquet_path, writer)
            for symbol, asset_class in self.symbols:
                for timeframe in timeframes:
                    bars = self.bars_loader(symbol, asset_class, timeframe)
                    if not bars:
                        logger.warning("BARS_UNAVAILABLE symbol=%s tf=%s",
                                       symbol, timeframe)
                        continue
                    tasks = self._build_tasks(symbol, asset_class, timeframe,
                                              bars, walks, combos, completed)
                    if not tasks:
                        continue
                    rows = Parallel(n_jobs=n_jobs, backend="loky")(
                        delayed(_run_one)(t) for t in tasks
                    )
                    self._write_rows(writer, rows)
        finally:
            writer.close()
        # Atomic swap of the rewritten file into place
        parquet_path.with_suffix(".parquet.tmp").replace(parquet_path)
        return parquet_path

    @staticmethod
    def _load_completed_keys(parquet_path: Path) -> set[tuple[str, str, int, str]]:
        if not parquet_path.exists():
            return set()
        df = pd.read_parquet(parquet_path,
                             columns=["symbol", "timeframe", "walk_idx", "fingerprint"])
        return set(zip(df["symbol"], df["timeframe"],
                       df["walk_idx"].astype(int), df["fingerprint"]))

    @staticmethod
    def _copy_existing_rows(parquet_path: Path, writer: pq.ParquetWriter) -> None:
        existing = pa.parquet.read_table(parquet_path, schema=_PARQUET_SCHEMA)
        writer.write_table(existing)

    def _build_tasks(self, symbol, asset_class, timeframe, bars, walks, combos,
                     completed):
        out: list[RunTask] = []
        for walk in walks:
            is_bars = _slice_bars(bars, walk.is_start, walk.is_end)
            oos_bars = _slice_bars(bars, walk.oos_start, walk.oos_end)
            if not is_bars or not oos_bars:
                continue
            for combo in combos:
                key = (symbol, timeframe, walk.idx, combo.fingerprint)
                if key in completed:
                    continue
                out.append(RunTask(
                    symbol=symbol, asset_class=asset_class, timeframe=timeframe,
                    walk=walk, is_bars=is_bars, oos_bars=oos_bars, combo=combo,
                    initial_equity=self.cfg["history"]["initial_equity"],
                    ac_configs=self.asset_class_configs,
                    risk_cfg=self.cfg["risk"],
                    filters_cfg=self.cfg["filters"],
                    min_trades=self.cfg["fitness"]["min_trades"],
                ))
        return out
```

- [ ] **Step 4: Run all runner tests, expect green**

Run: `.venv/bin/python -m pytest tests/test_wfo_runner.py -v`
Expected: 7 PASS.

- [ ] **Step 5: Commit**

```bash
git add backtest/wfo/runner.py tests/test_wfo_runner.py
git commit -m "feat(wfo): runner resumability — skip (sym,tf,walk,fp) keys already in parquet"
```

---

## Task 9: Report — aggregate + WFE gate

**Files:**
- Create: `backtest/wfo/report.py` (aggregator only)
- Test: `tests/test_wfo_report.py` (aggregation cases)

- [ ] **Step 1: Write the failing tests**

Write `tests/test_wfo_report.py`:

```python
import math

import pandas as pd

from backtest.wfo.report import GateConfig, aggregate_results


def _row(symbol="AAPL", tf="5Min", walk=0, setup="price_discovery",
         fingerprint="fp1", *,
         is_sharpe, oos_sharpe, oos_pnl=100.0, oos_trades=10,
         status="ok"):
    return {
        "symbol": symbol, "asset_class": "us_equity", "timeframe": tf,
        "walk_idx": walk, "setup": setup, "fingerprint": fingerprint,
        "combo_values_json": "{}",
        "is_sharpe": is_sharpe, "is_trades": 25, "is_pnl": 0.0,
        "is_score": is_sharpe,
        "oos_sharpe": oos_sharpe, "oos_trades": oos_trades, "oos_pnl": oos_pnl,
        "oos_max_dd": 0.05, "oos_avg_R": 1.0,
        "status": status, "error": "",
    }


def test_aggregate_picks_per_walk_is_best():
    df = pd.DataFrame([
        # walk 0 — fp_a wins IS
        _row(walk=0, fingerprint="fp_a", is_sharpe=2.0, oos_sharpe=1.5),
        _row(walk=0, fingerprint="fp_b", is_sharpe=1.0, oos_sharpe=0.5),
        # walk 1 — fp_b wins IS
        _row(walk=1, fingerprint="fp_a", is_sharpe=0.8, oos_sharpe=0.3),
        _row(walk=1, fingerprint="fp_b", is_sharpe=1.5, oos_sharpe=1.0),
    ])
    out = aggregate_results(df, GateConfig(wfe_min=0.5, require_positive_oos_pnl=True))
    assert len(out) == 1
    row = out.iloc[0]
    # WFE = (1.5 + 1.0) / (2.0 + 1.5) = 2.5/3.5 ≈ 0.714
    assert abs(row["wfe"] - (2.5 / 3.5)) < 1e-9
    assert row["passed"] is True


def test_aggregate_gate_fails_when_wfe_below_min():
    df = pd.DataFrame([
        _row(walk=0, is_sharpe=2.0, oos_sharpe=0.3),
        _row(walk=1, is_sharpe=2.0, oos_sharpe=0.3),
    ])
    out = aggregate_results(df, GateConfig(wfe_min=0.5, require_positive_oos_pnl=True))
    # WFE = 0.6/4.0 = 0.15 → fail
    assert out.iloc[0]["passed"] is False


def test_aggregate_gate_fails_on_negative_oos_pnl():
    df = pd.DataFrame([
        _row(walk=0, is_sharpe=2.0, oos_sharpe=1.5, oos_pnl=-50.0),
    ])
    out = aggregate_results(df, GateConfig(wfe_min=0.5, require_positive_oos_pnl=True))
    assert out.iloc[0]["passed"] is False


def test_aggregate_handles_zero_or_negative_is_sharpe_sum():
    df = pd.DataFrame([
        _row(walk=0, is_sharpe=-1.0, oos_sharpe=0.5),
        _row(walk=1, is_sharpe=-1.0, oos_sharpe=0.5),
    ])
    out = aggregate_results(df, GateConfig(wfe_min=0.5, require_positive_oos_pnl=True))
    # Σ IS Sharpe ≤ 0 → wfe = NaN, gate fails
    assert math.isnan(out.iloc[0]["wfe"])
    assert out.iloc[0]["passed"] is False


def test_aggregate_ignores_non_ok_status():
    df = pd.DataFrame([
        _row(walk=0, is_sharpe=2.0, oos_sharpe=1.5),
        _row(walk=0, fingerprint="other", is_sharpe=99.0, oos_sharpe=99.0,
             status="failed"),
        _row(walk=1, is_sharpe=1.5, oos_sharpe=1.0),
    ])
    out = aggregate_results(df, GateConfig(wfe_min=0.5, require_positive_oos_pnl=True))
    # Failed row must be excluded; same expected WFE as the 2-walk happy path
    assert abs(out.iloc[0]["wfe"] - (2.5 / 3.5)) < 1e-9


def test_aggregate_emits_one_row_per_setup_timeframe():
    df = pd.DataFrame([
        _row(setup="price_discovery", walk=0, is_sharpe=1.0, oos_sharpe=0.7),
        _row(setup="price_discovery", walk=1, is_sharpe=1.0, oos_sharpe=0.7),
        _row(setup="vwap_bounce", fingerprint="fp_v", walk=0,
             is_sharpe=2.0, oos_sharpe=1.5),
        _row(setup="vwap_bounce", fingerprint="fp_v", walk=1,
             is_sharpe=2.0, oos_sharpe=1.5),
    ])
    out = aggregate_results(df, GateConfig(wfe_min=0.5, require_positive_oos_pnl=True))
    assert set(out["setup"]) == {"price_discovery", "vwap_bounce"}
```

- [ ] **Step 2: Run tests, expect failure**

Run: `.venv/bin/python -m pytest tests/test_wfo_report.py -v`
Expected: ImportError.

- [ ] **Step 3: Implement aggregator**

Write `backtest/wfo/report.py`:

```python
"""WFO aggregation, gate, and live-overrides emission."""
from __future__ import annotations
import json
import logging
import math
import os
from dataclasses import dataclass
from pathlib import Path

import pandas as pd
import yaml

logger = logging.getLogger(__name__)


@dataclass
class GateConfig:
    wfe_min: float = 0.5
    require_positive_oos_pnl: bool = True


def aggregate_results(results: pd.DataFrame, gate: GateConfig) -> pd.DataFrame:
    """For each (symbol, timeframe, setup): pick per-walk IS-best, compute WFE,
    apply gate. Returns one row per group with aggregate stats + pass/fail."""
    ok = results[results["status"] == "ok"]
    if ok.empty:
        return pd.DataFrame(columns=[
            "symbol", "timeframe", "setup", "walks",
            "sum_is_sharpe", "sum_oos_sharpe", "wfe",
            "total_oos_pnl", "mean_oos_sharpe", "passed",
            "winning_fingerprint_last_walk",
        ])

    # Per-walk IS-best within each (symbol, timeframe, setup)
    keys = ["symbol", "timeframe", "setup"]
    winners = (
        ok.sort_values("is_sharpe", ascending=False)
          .groupby(keys + ["walk_idx"], as_index=False)
          .head(1)
    )

    rows = []
    for (sym, tf, stp), g in winners.groupby(keys):
        g = g.sort_values("walk_idx")
        sum_is = float(g["is_sharpe"].sum())
        sum_oos = float(g["oos_sharpe"].sum())
        total_oos_pnl = float(g["oos_pnl"].sum())
        wfe = sum_oos / sum_is if sum_is > 0 else math.nan
        passed = bool(
            (not math.isnan(wfe))
            and (wfe >= gate.wfe_min)
            and ((total_oos_pnl > 0) if gate.require_positive_oos_pnl else True)
        )
        last_walk_winner = g.iloc[-1]["fingerprint"]
        rows.append({
            "symbol": sym, "timeframe": tf, "setup": stp,
            "walks": int(len(g)),
            "sum_is_sharpe": sum_is, "sum_oos_sharpe": sum_oos,
            "wfe": wfe, "total_oos_pnl": total_oos_pnl,
            "mean_oos_sharpe": float(g["oos_sharpe"].mean()),
            "passed": passed,
            "winning_fingerprint_last_walk": last_walk_winner,
        })
    return pd.DataFrame(rows)
```

- [ ] **Step 4: Run tests, expect green**

Run: `.venv/bin/python -m pytest tests/test_wfo_report.py -v`
Expected: 6 PASS.

- [ ] **Step 5: Commit**

```bash
git add backtest/wfo/report.py tests/test_wfo_report.py
git commit -m "feat(wfo): aggregator — per-walk IS argmax + Pardo WFE + gate"
```

---

## Task 10: Report — emit `live_overrides.yaml` + `summary.md`

**Files:**
- Modify: `backtest/wfo/report.py` (append emitter functions)
- Modify: `tests/test_wfo_report.py` (append emission tests)

- [ ] **Step 1: Append failing emission tests**

Append to `tests/test_wfo_report.py`:

```python
import yaml

from backtest.wfo.report import emit_live_overrides, emit_summary_md


def _agg_passing():
    return pd.DataFrame([
        {"symbol": "AAPL", "timeframe": "15Min", "setup": "price_discovery",
         "walks": 30, "sum_is_sharpe": 30.0, "sum_oos_sharpe": 22.0,
         "wfe": 0.733, "total_oos_pnl": 4_213.5, "mean_oos_sharpe": 0.733,
         "passed": True, "winning_fingerprint_last_walk": "fp_apl"},
        {"symbol": "AAPL", "timeframe": "30Min", "setup": "vwap_bounce",
         "walks": 30, "sum_is_sharpe": 28.0, "sum_oos_sharpe": 14.0,
         "wfe": 0.5, "total_oos_pnl": 2_000.0, "mean_oos_sharpe": 0.467,
         "passed": True, "winning_fingerprint_last_walk": "fp_avb"},
        {"symbol": "TSLA", "timeframe": "5Min", "setup": "price_discovery",
         "walks": 30, "sum_is_sharpe": 10.0, "sum_oos_sharpe": 1.0,
         "wfe": 0.1, "total_oos_pnl": -500.0, "mean_oos_sharpe": 0.033,
         "passed": False, "winning_fingerprint_last_walk": "fp_tpd"},
    ])


def _last_walk_combos():
    """Map fingerprint → (setup_values, pm_values)."""
    return {
        "fp_apl": ({"atr_mult_stop": 1.25, "target_R": 2.0, "arm_window_bars": 6},
                   {"max_hold_bars": 12, "breakeven_at_R": 1.0}),
        "fp_avb": ({"atr_mult_stop": 1.5, "target_R": 2.5, "arm_window_bars": 4},
                   {"max_hold_bars": 8, "breakeven_at_R": 0.75}),
        "fp_tpd": ({"atr_mult_stop": 1.0}, {"max_hold_bars": 12, "breakeven_at_R": 1.0}),
    }


def test_emit_live_overrides_picks_highest_oos_sharpe_per_symbol(tmp_path):
    out_path = tmp_path / "live_overrides.yaml"
    emit_live_overrides(_agg_passing(), _last_walk_combos(), out_path,
                        run_id="2026-05-19T00-00_test", git_sha="b273796",
                        gate=GateConfig(wfe_min=0.5))
    data = yaml.safe_load(out_path.read_text())
    # AAPL → 15Min wins (mean_oos_sharpe 0.733 > 0.467)
    assert data["symbols"]["AAPL"]["timeframe"] == "15Min"
    assert data["symbols"]["AAPL"]["setup"] == "price_discovery"
    assert data["symbols"]["AAPL"]["setup_params"]["target_R"] == 2.0
    # TSLA failed → not present
    assert "TSLA" not in data["symbols"]


def test_emit_live_overrides_empty_when_none_pass(tmp_path):
    df = _agg_passing().assign(passed=False)
    out_path = tmp_path / "live_overrides.yaml"
    emit_live_overrides(df, _last_walk_combos(), out_path,
                        run_id="r", git_sha="s",
                        gate=GateConfig(wfe_min=0.5))
    data = yaml.safe_load(out_path.read_text())
    assert data["symbols"] == {}


def test_emit_summary_md_contains_all_groups(tmp_path):
    out_path = tmp_path / "summary.md"
    emit_summary_md(_agg_passing(), out_path,
                    run_id="r", git_sha="s",
                    gate=GateConfig(wfe_min=0.5))
    text = out_path.read_text()
    assert "AAPL" in text and "TSLA" in text
    assert "price_discovery" in text
    assert "passed" in text.lower()
```

- [ ] **Step 2: Run tests, expect failure**

Run: `.venv/bin/python -m pytest tests/test_wfo_report.py -k emit -v`
Expected: ImportError on emitters.

- [ ] **Step 3: Append emitters to `report.py`**

Append to `backtest/wfo/report.py`:

```python
def _atomic_write(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(payload)
    os.replace(tmp, path)


def emit_live_overrides(
    aggregated: pd.DataFrame,
    last_walk_combos: dict[str, tuple[dict, dict]],
    out_path: Path,
    *,
    run_id: str,
    git_sha: str,
    gate: GateConfig,
) -> None:
    """Per symbol, choose the highest mean_oos_sharpe among passed groups,
    tie-break on sorted (timeframe, setup). Emit YAML."""
    passed = aggregated[aggregated["passed"]].copy()
    symbols: dict[str, dict] = {}
    if not passed.empty:
        passed = passed.sort_values(
            ["mean_oos_sharpe", "timeframe", "setup"],
            ascending=[False, True, True],
        )
        for sym, g in passed.groupby("symbol"):
            winner = g.iloc[0]
            fp = winner["winning_fingerprint_last_walk"]
            if fp not in last_walk_combos:
                logger.warning("MISSING_LAST_WALK_COMBO symbol=%s fp=%s", sym, fp)
                continue
            setup_values, pm_values = last_walk_combos[fp]
            symbols[sym] = {
                "timeframe": str(winner["timeframe"]),
                "setup": str(winner["setup"]),
                "setup_params": setup_values,
                "position_management": pm_values,
                "metadata": {
                    "walks": int(winner["walks"]),
                    "mean_oos_sharpe": float(winner["mean_oos_sharpe"]),
                    "wfe": float(winner["wfe"]),
                    "total_oos_pnl": float(winner["total_oos_pnl"]),
                },
            }

    header = (
        f"# Generated by scripts/run_wfo.py\n"
        f"# run_id: {run_id}\n"
        f"# git_sha: {git_sha}\n"
        f"# wfe_min: {gate.wfe_min}  "
        f"require_positive_oos_pnl: {gate.require_positive_oos_pnl}\n"
        f"# gate-passed: {len(symbols)} of {aggregated['symbol'].nunique()} "
        f"evaluated symbols\n\n"
    )
    body = yaml.safe_dump({"symbols": symbols}, sort_keys=True, default_flow_style=False)
    _atomic_write(out_path, header + body)


def emit_summary_md(aggregated: pd.DataFrame, out_path: Path, *,
                    run_id: str, git_sha: str, gate: GateConfig) -> None:
    lines = [
        f"# WFO Run {run_id}\n",
        f"- git_sha: `{git_sha}`",
        f"- gate: WFE ≥ {gate.wfe_min}, "
        f"require_positive_oos_pnl: {gate.require_positive_oos_pnl}",
        f"- evaluated symbols: {aggregated['symbol'].nunique()}",
        f"- gate-passed groups: {int(aggregated['passed'].sum())}",
        "",
        "| symbol | timeframe | setup | walks | mean OOS Sharpe | WFE | total OOS P&L | passed |",
        "|---|---|---|---:|---:|---:|---:|:-:|",
    ]
    sorted_df = aggregated.sort_values(
        ["passed", "mean_oos_sharpe"], ascending=[False, False],
    )
    for _, r in sorted_df.iterrows():
        wfe = "—" if math.isnan(r["wfe"]) else f"{r['wfe']:.3f}"
        lines.append(
            f"| {r['symbol']} | {r['timeframe']} | {r['setup']} | "
            f"{int(r['walks'])} | {r['mean_oos_sharpe']:.3f} | {wfe} | "
            f"{r['total_oos_pnl']:.2f} | "
            f"{'✓' if r['passed'] else '✗'} |"
        )
    _atomic_write(out_path, "\n".join(lines) + "\n")
```

- [ ] **Step 4: Run tests, expect green**

Run: `.venv/bin/python -m pytest tests/test_wfo_report.py -v`
Expected: 9 PASS (6 prior + 3 new).

- [ ] **Step 5: Commit**

```bash
git add backtest/wfo/report.py tests/test_wfo_report.py
git commit -m "feat(wfo): emit live_overrides.yaml + summary.md (atomic writes)"
```

---

## Task 11: Report — `latest` symlink swap

**Files:**
- Modify: `backtest/wfo/report.py` (`update_latest_symlink`)
- Modify: `tests/test_wfo_report.py` (append symlink test)

- [ ] **Step 1: Append failing test**

Append to `tests/test_wfo_report.py`:

```python
def test_update_latest_symlink_atomic(tmp_path):
    from backtest.wfo.report import update_latest_symlink
    runs = tmp_path / "runs"
    run1 = runs / "run1"
    run2 = runs / "run2"
    run1.mkdir(parents=True)
    run2.mkdir(parents=True)
    latest = runs / "latest"

    update_latest_symlink(latest, run1)
    assert latest.is_symlink()
    assert latest.resolve() == run1.resolve()

    update_latest_symlink(latest, run2)
    assert latest.resolve() == run2.resolve()


def test_update_latest_symlink_skipped_when_zero_passed(tmp_path):
    from backtest.wfo.report import update_latest_symlink_if_passing
    runs = tmp_path / "runs"
    run1 = runs / "run1"
    run1.mkdir(parents=True)
    latest = runs / "latest"

    aggregated = _agg_passing().assign(passed=False)
    update_latest_symlink_if_passing(latest, run1, aggregated)
    assert not latest.exists()
```

- [ ] **Step 2: Run tests, expect failure**

Run: `.venv/bin/python -m pytest tests/test_wfo_report.py -k symlink -v`
Expected: ImportError.

- [ ] **Step 3: Append symlink helpers**

Append to `backtest/wfo/report.py`:

```python
def update_latest_symlink(latest: Path, target: Path) -> None:
    """Atomically point `latest` symlink at `target` (relative)."""
    latest.parent.mkdir(parents=True, exist_ok=True)
    tmp = latest.with_suffix(latest.suffix + ".tmp_link")
    if tmp.is_symlink() or tmp.exists():
        tmp.unlink()
    tmp.symlink_to(target.relative_to(latest.parent), target_is_directory=True)
    os.replace(tmp, latest)


def update_latest_symlink_if_passing(latest: Path, target: Path,
                                     aggregated: pd.DataFrame) -> bool:
    """Only swap `latest` when the run produced at least one passing row."""
    if aggregated["passed"].any():
        update_latest_symlink(latest, target)
        return True
    logger.info("LATEST_NOT_UPDATED reason=zero_passing_groups")
    return False
```

- [ ] **Step 4: Run tests, expect green**

Run: `.venv/bin/python -m pytest tests/test_wfo_report.py -v`
Expected: 11 PASS.

- [ ] **Step 5: Commit**

```bash
git add backtest/wfo/report.py tests/test_wfo_report.py
git commit -m "feat(wfo): atomic 'latest' symlink swap, gated on ≥1 passing group"
```

---

## Task 12: CLI — `scripts/run_wfo.py`

**Files:**
- Create: `scripts/__init__.py` (empty)
- Create: `scripts/run_wfo.py`
- Create: `config/wfo.yaml`
- Test: `tests/test_run_wfo_cli.py`

- [ ] **Step 1: Create `config/wfo.yaml` template**

Write `config/wfo.yaml`:

```yaml
# Walk-Forward Optimization meta-config.
# Loaded by scripts/run_wfo.py alongside config/settings.yaml.

run:
  output_root: runtime/wfo
  random_seed: 42
  parallelism: -1                    # joblib n_jobs; -1 = all cores

history:
  start: "2024-01-01"
  end: "2026-04-30"
  initial_equity: 100000

windowing:
  in_sample: "6mo"                   # parser: <int>(d|mo)
  out_of_sample: "1mo"
  step: null                         # null → step == out_of_sample

universe:
  source: alpaca_scan                # alpaca_scan | symbols
  symbols: []                        # used iff source: symbols
  alpaca_scan:
    classes: [us_equity, crypto]
    min_dollar_volume_20d: 5000000
    top_n_per_class:
      us_equity: 100
      crypto: null
    cache_dir: runtime/wfo/universe_cache

timeframes:
  - 5Min
  - 15Min
  - 30Min
  - 1Hour

fitness:
  metric: sharpe
  min_trades: 20

gate:
  wfe_min: 0.5
  require_positive_oos_pnl: true

grid:
  price_discovery:
    enabled: [true]
    atr_mult_stop:    [0.75, 1.0, 1.25, 1.5]
    target_R:         [1.0, 1.5, 2.0, 2.5]
    arm_window_bars:  [4, 6, 8]
    cooldown_bars:    [12]
  fade_extreme:
    enabled: [true]
    atr_mult_stop:    [0.5, 0.75, 1.0]
    scale_offsets_atr: [[0.0, 0.25, 0.5]]
    scale_weights:     [[0.4, 0.35, 0.25]]
    cooldown_bars:    [12]
  return_to_value:
    enabled: [true]
    atr_mult_stop:    [0.75, 1.0, 1.25]
    arm_window_bars:  [4, 6, 8]
    cooldown_bars:    [12]
  vwap_bounce:
    enabled: [true]
    atr_mult_stop:    [1.0, 1.25, 1.5]
    target_R:         [1.5, 2.0, 2.5]
    arm_window_bars:  [4, 6]
    cooldown_bars:    [8]

position_management:
  max_hold_bars:     [8, 12, 16]
  breakeven_at_R:    [0.75, 1.0, 1.25]
```

- [ ] **Step 2: Write the failing CLI test**

Write `tests/test_run_wfo_cli.py`:

```python
import subprocess
import sys
from pathlib import Path


def test_run_wfo_cli_help():
    result = subprocess.run(
        [sys.executable, "-m", "scripts.run_wfo", "--help"],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0
    assert "--config" in result.stdout
    assert "--settings" in result.stdout
```

- [ ] **Step 3: Implement scaffolding**

Write `scripts/__init__.py` empty (one newline).

Write `scripts/run_wfo.py`:

```python
"""WFO entry point. Run: python -m scripts.run_wfo --config config/wfo.yaml"""
from __future__ import annotations
import argparse
import hashlib
import json
import logging
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import yaml

from backtest.alpaca_data import AlpacaData                 # noqa: F401
from backtest.wfo.grid import expand_grid
from backtest.wfo.report import (
    GateConfig, aggregate_results, emit_live_overrides, emit_summary_md,
    update_latest_symlink_if_passing,
)
from backtest.wfo.runner import WFORunner
from backtest.wfo.universe import scan_alpaca_universe
from broker.alpaca_client import AlpacaClient
from broker.alpaca_data import AlpacaData
from core.asset_class import AssetClassConfig

logger = logging.getLogger("wfo")


def _git_sha() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], text=True
        ).strip()
    except Exception:
        return "unknown"


def _run_id(merged_cfg: dict) -> str:
    payload = json.dumps(merged_cfg, sort_keys=True, default=str)
    digest = hashlib.blake2b(payload.encode("utf-8"), digest_size=4).hexdigest()
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M")
    return f"{ts}_{digest}"


def _build_asset_class_configs(settings_cfg: dict, wfo_cfg: dict
                               ) -> dict[str, AssetClassConfig]:
    out: dict[str, AssetClassConfig] = {}
    timeframes = wfo_cfg["timeframes"]
    bar_timeframe = timeframes[0]      # finest is fine here; per-task overrides
    for name, raw in settings_cfg["asset_classes"].items():
        out[name] = AssetClassConfig(
            name=name,
            timezone=raw["timezone"],
            session_open_local=raw["session_open_local"],
            session_close_local=raw["session_close_local"],
            opening_blackout_min=settings_cfg["filters"]["opening_blackout_min"],
            bar_timeframe=bar_timeframe,
            slippage_bps=raw.get("slippage_bps", 0.0),
            commission_per_share=raw.get("commission_per_share", 0.0),
            commission_bps=raw.get("commission_bps", 0.0),
        )
    return out


def _resolve_universe(wfo_cfg: dict, client: AlpacaClient) -> list[tuple[str, str]]:
    src = wfo_cfg["universe"]["source"]
    if src == "symbols":
        return [(s, "us_equity" if "/" not in s else "crypto")
                for s in wfo_cfg["universe"]["symbols"]]
    if src == "alpaca_scan":
        scan = wfo_cfg["universe"]["alpaca_scan"]
        return scan_alpaca_universe(
            client,
            classes=scan["classes"],
            min_dollar_volume_20d=scan["min_dollar_volume_20d"],
            top_n_per_class=scan["top_n_per_class"],
            cache_dir=scan["cache_dir"],
        )
    raise ValueError(f"Unknown universe.source: {src!r}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Walk-Forward Optimization runner")
    parser.add_argument("--config", default="config/wfo.yaml",
                        help="WFO meta-config")
    parser.add_argument("--settings", default="config/settings.yaml",
                        help="Live settings (asset class definitions etc.)")
    parser.add_argument("--run-id", default=None,
                        help="Override deterministic run id (forces a fresh dir)")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    wfo_cfg = yaml.safe_load(Path(args.config).read_text())
    settings_cfg = yaml.safe_load(Path(args.settings).read_text())

    run_id = args.run_id or _run_id({"wfo": wfo_cfg, "settings": settings_cfg})
    output_dir = Path(wfo_cfg["run"]["output_root"]) / run_id
    output_dir.mkdir(parents=True, exist_ok=True)

    manifest_path = output_dir / "manifest.json"
    manifest = {
        "run_id": run_id,
        "git_sha": _git_sha(),
        "started_at": datetime.now(timezone.utc).isoformat(),
        "wfo_config_path": args.config,
        "settings_config_path": args.settings,
    }
    manifest_path.write_text(json.dumps(manifest, indent=2))
    logger.info("WFO_BOOT run_id=%s output_dir=%s", run_id, output_dir)

    client = AlpacaClient()
    data = AlpacaData(client, cache_dir=settings_cfg.get("backtest", {}
                                          ).get("cache_dir", "runtime/bars_cache"))
    universe = _resolve_universe(wfo_cfg, client)
    logger.info("WFO_UNIVERSE size=%d", len(universe))

    ac_configs = _build_asset_class_configs(settings_cfg, wfo_cfg)
    history = wfo_cfg["history"]

    def bars_loader(symbol: str, asset_class: str, timeframe: str):
        from datetime import datetime, timezone
        start = datetime.fromisoformat(str(history["start"])).replace(tzinfo=timezone.utc)
        end = datetime.fromisoformat(str(history["end"])).replace(tzinfo=timezone.utc)
        return data.get_bars(symbol, asset_class, timeframe,
                             start=start, end=end, use_cache=True)

    runner_cfg = {
        "run": wfo_cfg["run"],
        "history": history,
        "windowing": wfo_cfg["windowing"],
        "timeframes": wfo_cfg["timeframes"],
        "fitness": wfo_cfg["fitness"],
        "gate": wfo_cfg["gate"],
        "grid": wfo_cfg["grid"],
        "position_management": wfo_cfg["position_management"],
        "risk": settings_cfg["risk"],
        "filters": settings_cfg["filters"],
    }

    runner = WFORunner(
        cfg=runner_cfg, asset_class_configs=ac_configs,
        symbols=universe, bars_loader=bars_loader, output_dir=output_dir,
    )
    parquet_path = runner.run()

    # Aggregate + emit
    df = pd.read_parquet(parquet_path)
    last_walk_combos = _build_last_walk_combos_index(df, runner_cfg)
    aggregated = aggregate_results(
        df, GateConfig(wfe_min=wfo_cfg["gate"]["wfe_min"],
                       require_positive_oos_pnl=wfo_cfg["gate"]["require_positive_oos_pnl"]),
    )
    emit_live_overrides(aggregated, last_walk_combos,
                        output_dir / "live_overrides.yaml",
                        run_id=run_id, git_sha=manifest["git_sha"],
                        gate=GateConfig(**wfo_cfg["gate"]))
    emit_summary_md(aggregated, output_dir / "summary.md",
                    run_id=run_id, git_sha=manifest["git_sha"],
                    gate=GateConfig(**wfo_cfg["gate"]))

    latest = Path(wfo_cfg["run"]["output_root"]) / "latest"
    update_latest_symlink_if_passing(latest, output_dir, aggregated)

    manifest["completed_at"] = datetime.now(timezone.utc).isoformat()
    manifest["evaluated_groups"] = int(len(aggregated))
    manifest["passed_groups"] = int(aggregated["passed"].sum() if not aggregated.empty else 0)
    manifest_path.write_text(json.dumps(manifest, indent=2))
    logger.info("WFO_DONE run_id=%s passed=%d / %d",
                run_id, manifest["passed_groups"], manifest["evaluated_groups"])
    return 0


def _build_last_walk_combos_index(df: pd.DataFrame, runner_cfg: dict
                                  ) -> dict[str, tuple[dict, dict]]:
    """For every fingerprint in df, look up its (setup_values, pm_values).

    Cheap: rebuild the grid once and index by fingerprint.
    """
    combos = expand_grid(runner_cfg["grid"], runner_cfg["position_management"])
    return {c.fingerprint: (c.setup_values, c.pm_values) for c in combos}


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run CLI help test**

Run: `.venv/bin/python -m pytest tests/test_run_wfo_cli.py -v`
Expected: 1 PASS (the help text loads without invoking the broker).

- [ ] **Step 5: Commit**

```bash
git add scripts/__init__.py scripts/run_wfo.py config/wfo.yaml tests/test_run_wfo_cli.py
git commit -m "feat(wfo): scripts/run_wfo.py CLI + config/wfo.yaml template"
```

---

## Task 13: `main.py` — `apply_overrides` helper + `settings.yaml` `overrides:` block

**Files:**
- Modify: `main.py` (add `apply_overrides`)
- Modify: `config/settings.yaml` (add `overrides:` block)
- Test: `tests/test_main_overrides.py`

- [ ] **Step 1: Write the failing tests**

Write `tests/test_main_overrides.py`:

```python
import os

import yaml

os.environ.setdefault("ALPACA_API_KEY", "test")
os.environ.setdefault("ALPACA_SECRET_KEY", "test")

from main import apply_overrides


def test_apply_overrides_returns_cfg_when_path_missing(tmp_path):
    cfg = {"setups": {"price_discovery": {"enabled": True}}}
    out = apply_overrides(cfg, overrides_path=None)
    assert out is cfg
    assert "_per_symbol_overrides" not in out


def test_apply_overrides_returns_cfg_when_file_absent(tmp_path):
    cfg = {"setups": {}}
    out = apply_overrides(cfg, overrides_path=str(tmp_path / "missing.yaml"))
    assert "_per_symbol_overrides" not in out


def test_apply_overrides_loads_per_symbol_map(tmp_path):
    overrides_path = tmp_path / "live_overrides.yaml"
    overrides_path.write_text(yaml.safe_dump({
        "symbols": {
            "AAPL": {
                "timeframe": "15Min",
                "setup": "price_discovery",
                "setup_params": {"atr_mult_stop": 1.25, "target_R": 2.0,
                                 "arm_window_bars": 6},
                "position_management": {"max_hold_bars": 12, "breakeven_at_R": 1.0},
            },
        },
    }))
    cfg = {"setups": {}}
    out = apply_overrides(cfg, str(overrides_path))
    assert "AAPL" in out["_per_symbol_overrides"]
    assert out["_per_symbol_overrides"]["AAPL"]["timeframe"] == "15Min"


def test_apply_overrides_disabled_flag_short_circuits(tmp_path):
    overrides_path = tmp_path / "live_overrides.yaml"
    overrides_path.write_text(yaml.safe_dump({"symbols": {"AAPL": {}}}))
    cfg = {"setups": {}}
    out = apply_overrides(cfg, str(overrides_path), enabled=False)
    assert "_per_symbol_overrides" not in out
```

- [ ] **Step 2: Run tests, expect failure**

Run: `.venv/bin/python -m pytest tests/test_main_overrides.py -v`
Expected: ImportError on `apply_overrides`.

- [ ] **Step 3: Add `apply_overrides` to `main.py`**

In `main.py`, immediately after the `def load_config(path: str = "config/settings.yaml") -> dict:` block, add:

```python
def apply_overrides(cfg: dict, overrides_path: str | None,
                    *, enabled: bool = True) -> dict:
    """Layer per-symbol WFO overrides on top of the loaded settings."""
    if not enabled or not overrides_path:
        return cfg
    from pathlib import Path
    if not Path(overrides_path).exists():
        return cfg
    payload = yaml.safe_load(Path(overrides_path).read_text()) or {}
    symbols = payload.get("symbols") or {}
    if symbols:
        cfg["_per_symbol_overrides"] = symbols
    return cfg
```

- [ ] **Step 4: Add `overrides:` block to `config/settings.yaml`**

Append at the end of `config/settings.yaml`:

```yaml

overrides:
  path: runtime/wfo/latest/live_overrides.yaml
  enabled: true
```

- [ ] **Step 5: Wire `apply_overrides` into `main()`**

In `main.py`, replace the `cfg = load_config()` line (inside `def main():`) with:

```python
    cfg = load_config()
    overrides_cfg = cfg.get("overrides") or {}
    cfg = apply_overrides(cfg, overrides_cfg.get("path"),
                          enabled=overrides_cfg.get("enabled", True))
```

- [ ] **Step 6: Run tests + smoke import, expect green**

Run: `.venv/bin/python -m pytest tests/test_main_overrides.py -v`
Run: `TRADING_ENV=test ALPACA_API_KEY=x ALPACA_SECRET_KEY=x .venv/bin/python -c "import main; print('ok')"`
Expected: 4 PASS; `ok`.

- [ ] **Step 7: Commit**

```bash
git add main.py config/settings.yaml tests/test_main_overrides.py
git commit -m "feat(wfo): main.py apply_overrides + settings.yaml overrides block"
```

---

## Task 14: `main.py` — `build_setups` honors per-symbol override

**Files:**
- Modify: `main.py` (`build_setups`)
- Modify: `tests/test_main_overrides.py` (append)

- [ ] **Step 1: Append failing test**

Append to `tests/test_main_overrides.py`:

```python
def test_build_setups_uses_override_for_overridden_symbol():
    from main import build_setups
    cfg = {
        "setups": {
            "price_discovery": {"enabled": True, "atr_mult_stop": 0.5,
                                "target_R": 1.0, "arm_window_bars": 6,
                                "cooldown_bars": 12},
            "fade_extreme": {"enabled": True, "atr_mult_stop": 0.75,
                             "scale_offsets_atr": [0.0, 0.25, 0.5],
                             "scale_weights": [0.4, 0.35, 0.25],
                             "cooldown_bars": 12},
            "return_to_value": {"enabled": True, "atr_mult_stop": 1.0,
                                "arm_window_bars": 6, "cooldown_bars": 12},
            "vwap_bounce": {"enabled": True, "atr_mult_stop": 1.25,
                            "target_R": 2.0, "arm_window_bars": 4,
                            "cooldown_bars": 8},
        },
        "_per_symbol_overrides": {
            "AAPL": {
                "timeframe": "15Min",
                "setup": "price_discovery",
                "setup_params": {"atr_mult_stop": 1.25, "target_R": 2.0,
                                 "arm_window_bars": 6},
                "position_management": {"max_hold_bars": 12, "breakeven_at_R": 1.0},
            },
        },
    }
    aapl = build_setups(cfg, "AAPL")
    assert len(aapl) == 1
    s = aapl[0]
    assert type(s).__name__ == "PriceDiscoverySetup"
    assert s.atr_mult_stop == 1.25
    assert s.target_R == 2.0

    # Non-overridden symbol still gets all setups with global params
    spy = build_setups(cfg, "SPY")
    assert len(spy) == 4
```

- [ ] **Step 2: Run test, expect failure**

Run: `.venv/bin/python -m pytest tests/test_main_overrides.py::test_build_setups_uses_override_for_overridden_symbol -v`
Expected: FAIL — current `build_setups` ignores overrides.

- [ ] **Step 3: Modify `build_setups` in `main.py`**

Replace the existing `def build_setups(cfg: dict, symbol: str):` body with:

```python
def build_setups(cfg: dict, symbol: str):
    overrides = cfg.get("_per_symbol_overrides") or {}
    if symbol in overrides:
        return _build_setups_from_override(symbol, overrides[symbol])
    s = cfg["setups"]
    setups = []
    if s["price_discovery"]["enabled"]:
        setups.append(PriceDiscoverySetup(
            symbol,
            atr_mult_stop=s["price_discovery"]["atr_mult_stop"],
            target_R=s["price_discovery"]["target_R"],
            arm_window_bars=s["price_discovery"]["arm_window_bars"],
        ))
    if s["fade_extreme"]["enabled"]:
        setups.append(FadeExtremeSetup(
            symbol,
            atr_mult_stop=s["fade_extreme"]["atr_mult_stop"],
            scale_offsets_atr=s["fade_extreme"]["scale_offsets_atr"],
            scale_weights=s["fade_extreme"]["scale_weights"],
        ))
    if s["return_to_value"]["enabled"]:
        setups.append(ReturnToValueSetup(
            symbol,
            atr_mult_stop=s["return_to_value"]["atr_mult_stop"],
            arm_window_bars=s["return_to_value"]["arm_window_bars"],
        ))
    if s["vwap_bounce"]["enabled"]:
        setups.append(VWAPBounceSetup(
            symbol,
            atr_mult_stop=s["vwap_bounce"]["atr_mult_stop"],
            target_R=s["vwap_bounce"]["target_R"],
            arm_window_bars=s["vwap_bounce"]["arm_window_bars"],
        ))
    return setups


_OVERRIDE_FACTORIES = {
    "price_discovery": lambda symbol, p: PriceDiscoverySetup(
        symbol,
        atr_mult_stop=p["atr_mult_stop"],
        target_R=p["target_R"],
        arm_window_bars=p["arm_window_bars"],
    ),
    "fade_extreme": lambda symbol, p: FadeExtremeSetup(
        symbol,
        atr_mult_stop=p["atr_mult_stop"],
        scale_offsets_atr=p.get("scale_offsets_atr", [0.0, 0.25, 0.5]),
        scale_weights=p.get("scale_weights", [0.4, 0.35, 0.25]),
    ),
    "return_to_value": lambda symbol, p: ReturnToValueSetup(
        symbol,
        atr_mult_stop=p["atr_mult_stop"],
        arm_window_bars=p["arm_window_bars"],
    ),
    "vwap_bounce": lambda symbol, p: VWAPBounceSetup(
        symbol,
        atr_mult_stop=p["atr_mult_stop"],
        target_R=p["target_R"],
        arm_window_bars=p["arm_window_bars"],
    ),
}


def _build_setups_from_override(symbol: str, override: dict):
    setup_name = override["setup"]
    factory = _OVERRIDE_FACTORIES.get(setup_name)
    if factory is None:
        raise ValueError(f"Unknown setup in override for {symbol}: {setup_name!r}")
    return [factory(symbol, override["setup_params"])]
```

- [ ] **Step 4: Run tests, expect green**

Run: `.venv/bin/python -m pytest tests/test_main_overrides.py -v`
Expected: 5 PASS.

- [ ] **Step 5: Commit**

```bash
git add main.py tests/test_main_overrides.py
git commit -m "feat(wfo): main.py build_setups respects per-symbol override"
```

---

## Task 15: `main.py` — `position_manager_for` factory

**Files:**
- Modify: `main.py`
- Modify: `tests/test_main_overrides.py`

- [ ] **Step 1: Append failing test**

Append to `tests/test_main_overrides.py`:

```python
def test_position_manager_for_uses_override_values():
    from main import position_manager_for
    from state.position_book import PositionBook

    cfg = {
        "position_management": {"max_hold_bars": 12, "breakeven_at_R": 1.0},
        "_per_symbol_overrides": {
            "AAPL": {"timeframe": "15Min", "setup": "price_discovery",
                     "setup_params": {},
                     "position_management": {"max_hold_bars": 8,
                                             "breakeven_at_R": 0.75}},
        },
    }
    book = PositionBook()
    aapl_pm = position_manager_for("AAPL", cfg, book)
    assert aapl_pm._max_hold_bars == 8
    assert aapl_pm._breakeven_at_R == 0.75

    spy_pm = position_manager_for("SPY", cfg, book)
    assert spy_pm._max_hold_bars == 12
    assert spy_pm._breakeven_at_R == 1.0
```

- [ ] **Step 2: Run test, expect failure**

Run: `.venv/bin/python -m pytest tests/test_main_overrides.py::test_position_manager_for_uses_override_values -v`
Expected: FAIL — `position_manager_for` not yet defined.

- [ ] **Step 3: Add factory + thread it through engine wiring**

In `main.py`, immediately after `_build_setups_from_override` (added in Task 14), insert:

```python
def position_manager_for(symbol: str, cfg: dict, book) -> PositionManager:
    overrides = cfg.get("_per_symbol_overrides") or {}
    pm_cfg = (overrides.get(symbol, {}).get("position_management")
              if symbol in overrides else cfg["position_management"])
    return PositionManager(
        book,
        max_hold_bars=pm_cfg["max_hold_bars"],
        breakeven_at_R=pm_cfg["breakeven_at_R"],
    )
```

In `def main():`, replace the existing `pm = PositionManager(...)` and `engine = VWAPWaveEngine(...)` block with:

```python
    # When overrides exist, each symbol may want its own PositionManager. The
    # engine still receives a single PM; we wire a dispatcher that routes
    # on_bar(symbol, bar) to the right per-symbol PM.
    overrides = cfg.get("_per_symbol_overrides") or {}
    if overrides:
        per_symbol_pms = {sym: position_manager_for(sym, cfg, book)
                          for sym, _ in symbols}
        pm = _PerSymbolPositionManager(per_symbol_pms,
                                       fallback=position_manager_for("__default__",
                                                                     cfg, book))
    else:
        pm = position_manager_for("__default__", cfg, book)

    engine = VWAPWaveEngine(
        symbols=symbols, contexts=contexts, setups=setups,
        risk_manager=rm, executor=executor, book=book, ledger=ledger,
        position_manager=pm,
    )
```

Add the dispatcher class somewhere above `def main():`:

```python
class _PerSymbolPositionManager:
    """Routes on_bar(symbol, bar) to a per-symbol PositionManager."""

    def __init__(self, per_symbol: dict, fallback):
        self._per_symbol = per_symbol
        self._fallback = fallback

    def on_bar(self, symbol, bar):
        pm = self._per_symbol.get(symbol, self._fallback)
        return pm.on_bar(symbol, bar)
```

- [ ] **Step 4: Run tests + smoke import, expect green**

Run: `.venv/bin/python -m pytest tests/test_main_overrides.py -v`
Run: `TRADING_ENV=test ALPACA_API_KEY=x ALPACA_SECRET_KEY=x .venv/bin/python -c "import main; print('ok')"`
Expected: 6 PASS; `ok`.

- [ ] **Step 5: Commit**

```bash
git add main.py tests/test_main_overrides.py
git commit -m "feat(wfo): per-symbol PositionManager dispatcher when overrides present"
```

---

## Task 16: `main.py` — finest-timeframe scheduler + per-symbol bar fetch

**Files:**
- Modify: `main.py`
- Modify: `tests/test_main_overrides.py`

- [ ] **Step 1: Append failing test**

Append to `tests/test_main_overrides.py`:

```python
def test_timeframe_for_resolves_override_then_default():
    from main import timeframe_for
    cfg = {
        "scheduler": {"bar_timeframe": "5Min"},
        "_per_symbol_overrides": {"AAPL": {"timeframe": "15Min"}},
    }
    assert timeframe_for("AAPL", cfg) == "15Min"
    assert timeframe_for("SPY", cfg) == "5Min"


def test_finest_timeframe_picks_shortest_period():
    from main import finest_timeframe
    cfg = {
        "scheduler": {"bar_timeframe": "5Min"},
        "_per_symbol_overrides": {
            "AAPL": {"timeframe": "15Min"},
            "BTC/USD": {"timeframe": "30Min"},
        },
    }
    symbols = [("AAPL", "us_equity"), ("BTC/USD", "crypto"), ("SPY", "us_equity")]
    assert finest_timeframe(symbols, cfg) == "5Min"
```

- [ ] **Step 2: Run test, expect failure**

Run: `.venv/bin/python -m pytest tests/test_main_overrides.py -k timeframe -v`
Expected: FAIL — helpers not defined.

- [ ] **Step 3: Add `timeframe_for` + `finest_timeframe` helpers**

In `main.py`, immediately after `position_manager_for`, add:

```python
def timeframe_for(symbol: str, cfg: dict) -> str:
    overrides = cfg.get("_per_symbol_overrides") or {}
    if symbol in overrides and "timeframe" in overrides[symbol]:
        return overrides[symbol]["timeframe"]
    return cfg["scheduler"]["bar_timeframe"]


def finest_timeframe(symbols: list[tuple[str, str]], cfg: dict) -> str:
    from scheduler.bar_clock import parse_timeframe_minutes
    candidates = {timeframe_for(sym, cfg) for sym, _ in symbols}
    return min(candidates, key=parse_timeframe_minutes)
```

- [ ] **Step 4: Wire per-symbol timeframe into the scheduler loop**

In `def main():`, replace the existing scheduler block (the part starting `timeframe = cfg["scheduler"]["bar_timeframe"]` through the `engine.tick(...)` call inside the `try:`) with:

```python
    timeframe = finest_timeframe(symbols, cfg)
    grace = cfg["scheduler"]["wake_grace_seconds"]
    logger.info("vwap_wave loop starting; symbols=%d finest_tf=%s",
                len(symbols), timeframe)

    while not _shutdown:
        now = datetime.now(timezone.utc)
        target = next_boundary(now, timeframe, grace_seconds=grace)
        sleep_until(target)
        if _shutdown:
            break

        try:
            cycle_now = datetime.now(timezone.utc)
            fresh_bars: dict[str, list] = {}
            for sym, ac_name in symbols:
                ctx = contexts[sym]
                ac = ac_configs[ac_name]
                sym_tf = timeframe_for(sym, cfg)
                start = session_start_for(cycle_now, ac)
                bars = data.get_bars(sym, ac_name, sym_tf,
                                     start=start, end=cycle_now, use_cache=False)
                last_known_ts = ctx.bars[-1].ts if ctx.bars else None
                new_bars = [b for b in bars if last_known_ts is None or b.ts > last_known_ts]
                if new_bars:
                    fresh_bars[sym] = new_bars
            engine.tick(now=cycle_now, fresh_bars=fresh_bars)
```

- [ ] **Step 5: Run tests + smoke import + full suite, expect green**

Run: `.venv/bin/python -m pytest tests/test_main_overrides.py -v`
Run: `TRADING_ENV=test ALPACA_API_KEY=x ALPACA_SECRET_KEY=x .venv/bin/python -c "import main; print('ok')"`
Run: `.venv/bin/python -m pytest`
Expected: 8 PASS in test_main_overrides.py; `ok`; full suite green.

- [ ] **Step 6: Commit**

```bash
git add main.py tests/test_main_overrides.py
git commit -m "feat(wfo): per-symbol timeframe — scheduler ticks at finest, fetches per-symbol native"
```

---

## Task 17: Final green check + README pointer

**Files:**
- Modify: `README.md` (one-section addition pointing at WFO)

- [ ] **Step 1: Append WFO section to README**

Add after the existing "Backtest" section in `README.md`:

```markdown
### Walk-Forward Optimization

```bash
python -m scripts.run_wfo --config config/wfo.yaml
```

Tunes setup parameters per `(symbol, timeframe)` over rolling IS/OOS windows
across the broker's tradable universe. Output lands under
`runtime/wfo/<run_id>/`:

- `results.parquet` — every `(symbol, timeframe, walk, combo)` IS/OOS row.
- `live_overrides.yaml` — per-symbol best `(timeframe, setup, params)` for
  symbols whose aggregate **WFE ≥ 0.5** AND total OOS P&L > 0 (Pardo gate).
- `summary.md` — ranked human-readable table.

`runtime/wfo/latest` is a symlink the CLI updates on success. `main.py` reads
`runtime/wfo/latest/live_overrides.yaml` at boot when present and layers it on
top of `config/settings.yaml`. No silent overwrite — operator promotes by
manually editing `settings.yaml`.

Tunables: `config/wfo.yaml` (universe scan, IS/OOS lengths in days/months,
parameter grids per setup, fitness floor, gate thresholds).
```

- [ ] **Step 2: Final verifications**

Run: `.venv/bin/python -m pytest`
Run: `TRADING_ENV=test ALPACA_API_KEY=x ALPACA_SECRET_KEY=x .venv/bin/python -c "import main; print('ok')"`
Run: `.venv/bin/python -m scripts.run_wfo --help`
Expected: full suite green; `ok`; CLI help text printed.

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "docs: link the WFO runner from README"
```

---

## Execution notes

- **Suite budget:** every task ends with `.venv/bin/python -m pytest` ideally green; the final task confirms.
- **Resumability assumes deterministic `run_id`:** if a task list changes the merged config (e.g., new YAML key), the `run_id` hash changes and a fresh dir is created — that's by design.
- **No engine code touched:** `scheduler/loop.py`, `backtest/intraday_replay.py`, `core/position_manager.py`, and the setups/filters are read-only in this plan. Any modification is a sign you've gone off-spec.
- **Spec reference:** `docs/superpowers/specs/2026-05-19-walk-forward-optimization-design.md` for any clarification.
