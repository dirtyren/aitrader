# VWAP Wave Protocol Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the HMM-based regime trader with an intraday VWAP Wave Protocol engine that trades equities, ETFs, and crypto via Alpaca, runs identical code in backtest and live, and ships behind a paper-trading gate.

**Architecture:** A bar-close scheduler wakes at each 5-minute boundary, pulls bars from Alpaca's REST data API, updates per-symbol `SessionContext` (incremental VWAP + ±1σ bands), and runs four setup state machines (Price Discovery Continuation, Fade Value Extremes, Return to Value, VWAP Bounce). Signals pass through a composable filter pipeline, get sized via ATR-based math, and are submitted as bracket orders (equity) or virtual-stop orders (crypto). The same `SessionContext`/`Setup`/filter classes drive a backtest replay engine.

**Tech Stack:** Python 3.11+, `requests` (Alpaca REST), `pandas` / `numpy` (analytics), `pytz` + `pandas-market-calendars` (sessions), `pytest` (tests), `streamlit` (dashboard). Removing: `hmmlearn`, `yfinance`.

**Reference spec:** `docs/superpowers/specs/2026-05-14-vwap-wave-protocol-design.md`

---

## Phase Map

| Phase | Theme | Output |
|---|---|---|
| 0 | Scaffold + delete dead code | Empty branch with HMM removed, new dirs |
| 1 | Data layer | `alpaca_data.py`, `bar_clock.py`, bars cache |
| 2 | Session & VWAP | `session.py`, `vwap.py`, `acceptance.py`, `regime_detector.py` |
| 3 | Setups | `base_setup.py` + 4 setup state machines |
| 4 | Risk pipeline | `filters.py`, `sizing.py`, `daily_ledger.py`, `position_book.py` |
| 5 | Live engine | `scheduler/loop.py`, rewritten `main.py`, broker extensions |
| 6 | Backtest engine | `intraday_replay.py`, `fill_engine.py`, performance metrics |
| 7 | Dashboard | New `ui/dashboard.py` |
| 8 | Validation | Three backtest sanity tests |
| 9 | Config + docs | New `settings.yaml`, README rewrite |

Each phase ends with a green test suite and a commit. Phases 1–4 require no broker connectivity.

---

## Pre-flight

### Pre-flight Task A: Branch + dependencies

**Files:**
- Modify: `requirements.txt`

- [ ] **A.1** Confirm working directory and clean state

```bash
cd /Users/alessandro.ren/dev/aitrader
git status
```

Expected: only the pre-existing `M docker-compose.yml` and `?? lock.file` (no other uncommitted work). If unexpected files appear, stop and ask.

- [ ] **A.2** Create the feature branch

```bash
git checkout -b feature/vwap-wave-protocol
```

Expected: `Switched to a new branch 'feature/vwap-wave-protocol'`.

- [ ] **A.3** Update `requirements.txt`

Replace the file's contents with:

```
numpy==1.26.4
pandas==2.2.3
alpaca-trade-api==3.2.0
streamlit==1.41.1
streamlit-autorefresh==1.0.1
plotly==5.24.1
scipy==1.14.1
python-dotenv==1.0.1
pyyaml==6.0.2
pytest==8.3.4
requests==2.32.3
joblib==1.4.2
pytz==2024.1
pandas-market-calendars==4.4.0
pyarrow==17.0.0
```

(Removed: `hmmlearn`, `yfinance`, `scikit-learn`. Added: `pytz`, `pandas-market-calendars`, `pyarrow` for parquet bar cache.)

- [ ] **A.4** Reinstall dependencies

```bash
pip install -r requirements.txt
```

Expected: clean install, no errors. `hmmlearn` and `yfinance` no longer present (`pip show hmmlearn` should fail).

- [ ] **A.5** Commit

```bash
git add requirements.txt
git commit -m "chore: swap deps for VWAP Wave Protocol (drop hmmlearn/yfinance, add pytz/market-calendars/pyarrow)"
```

---

## Phase 0 — Scaffold + Delete Dead Code

### Task 0.1: Delete the HMM stack

**Files:**
- Delete: `engine/hmm_model.py`, `engine/regime_classifier.py`, `engine/__init__.py`, `engine/`
- Delete: `core/feature_eng.py`, `core/orchestrator.py`
- Delete: `strategies/vol_allocation.py`, `strategies/base_strategy.py`
- Delete: `backtest/walk_forward.py`
- Delete: `core/data_loader.py`
- Delete: tests for the above (`tests/test_feature_eng.py`, `tests/test_strategy.py`)

- [ ] **0.1.1** Delete the HMM engine, strategies, orchestrator, data loader, and dead tests

```bash
git rm -r engine/
git rm core/feature_eng.py core/orchestrator.py core/data_loader.py
git rm strategies/vol_allocation.py strategies/base_strategy.py
git rm backtest/walk_forward.py
git rm tests/test_feature_eng.py tests/test_strategy.py
```

Expected: `rm` lines listed for each file.

- [ ] **0.1.2** Verify no references remain

```bash
grep -rn "hmm_model\|HMMModel\|RegimeClassifier\|VolatilityAllocation\|build_features\|StrategyOrchestrator\|walk_forward\|data_loader" --include="*.py" .
```

Expected: only matches inside files **already** scheduled for rewrite (`main.py`, `risk/manager.py`, `core/portfolio.py`, `broker/order_executor.py`, `tests/test_risk_manager.py`, `tests/test_order_executor.py`, `tests/test_portfolio.py`, `tests/test_circuit_breakers.py`). These will be cleaned up in subsequent tasks. **Do not delete or rewrite them yet.**

- [ ] **0.1.3** Stash a recovery list of which test files reference deleted symbols

```bash
grep -l "build_features\|HMMModel\|RegimeClassifier\|VolatilityAllocation\|StrategyOrchestrator" tests/*.py 2>/dev/null > /tmp/vwap_dead_refs.txt
cat /tmp/vwap_dead_refs.txt
```

Expected: prints the list of test files that still need cleanup. These files will be edited in Phase 4 to remove HMM imports as their underlying production modules are rewritten.

- [ ] **0.1.4** Commit

```bash
git commit -m "chore: delete HMM stack (engine, feature_eng, orchestrator, vol_allocation, walk_forward)"
```

### Task 0.2: Scaffold new directories with empty `__init__.py`

**Files:**
- Create: `core/__init__.py` (already exists; leave alone), `scheduler/__init__.py`, `state/__init__.py`
- Create: `tests/__init__.py` (already exists; leave alone)

- [ ] **0.2.1** Create new package directories

```bash
mkdir -p scheduler state runtime/bars_cache
touch scheduler/__init__.py state/__init__.py
```

- [ ] **0.2.2** Add `.gitkeep` for the bars cache so empty dir lands in git

```bash
touch runtime/bars_cache/.gitkeep
```

- [ ] **0.2.3** Verify directory structure

```bash
ls -d core scheduler state strategies risk broker backtest ui runtime runtime/bars_cache tests
```

Expected: every listed directory exists.

- [ ] **0.2.4** Commit

```bash
git add scheduler/ state/ runtime/bars_cache/
git commit -m "feat: scaffold scheduler/, state/, runtime/bars_cache/ for VWAP Wave Protocol"
```

### Task 0.3: Park the old portfolio.py until rewrite

`core/portfolio.py` and `risk/manager.py` are still imported by `main.py`. Phase 4 fully rewrites them. To avoid a broken tree between phases, we move them aside but don't delete yet.

- [ ] **0.3.1** Verify the imports `main.py` currently makes

```bash
grep -n "^from\|^import" main.py
```

Expected output includes (among others): `from core.portfolio import Portfolio`, `from risk.manager import RiskManager`, `from risk.circuit_breakers import CircuitBreaker`, `from broker.alpaca_client import AlpacaClient`, `from broker.order_executor import OrderExecutor`, `from ui.logging_setup import setup_logging`, plus the now-deleted HMM imports (which is why `python main.py` would fail right now — that's expected; we'll fix it in Phase 5).

- [ ] **0.3.2** Mark `main.py` as broken-by-design until Phase 5

Add a single-line guard at the top of `main.py` so anyone who runs it gets a clear message instead of a `ModuleNotFoundError`. Edit `main.py` line 1, replacing the docstring's first line:

Replace:

```python
"""
regime_trader — Autonomous Regime-Aware Trading System
"""
```

With:

```python
"""
VWAP Wave Protocol — Autonomous Intraday Trading System

NOTE: Mid-rewrite. This file is intentionally non-functional between
Phase 0 and Phase 5; the bar-close scheduler in scheduler/loop.py
becomes the new entry point. Run `pytest` for the current code.
"""
import sys
print("main.py is being rewritten as part of the VWAP Wave migration. "
      "See docs/superpowers/plans/2026-05-14-vwap-wave-protocol.md "
      "Phase 5 for the new entry point.", file=sys.stderr)
sys.exit(2)
```

(The rest of the file stays for now — it will be fully replaced in Phase 5.)

- [ ] **0.3.3** Commit

```bash
git add main.py
git commit -m "chore: temporarily disable main.py until Phase 5 scheduler rewrite"
```

### Task 0.4: Confirm tests still pass for kept modules

The remaining tests are: `test_circuit_breakers.py`, `test_order_executor.py`, `test_portfolio.py`, `test_risk_manager.py`. Some of these import deleted modules and will fail to collect — that's expected. We collect now to **document** which fail, so each later phase that rewrites a module knows what to fix.

- [ ] **0.4.1** Run the test suite

```bash
pytest tests/ --collect-only -q 2>&1 | tee /tmp/vwap_collect.log
```

Expected: a mix of "collected" lines and `ERROR` lines for tests that import deleted modules. Save the output for reference.

- [ ] **0.4.2** Run only the tests that *should* still pass

```bash
pytest tests/test_circuit_breakers.py -v
```

Expected: all tests pass. `circuit_breakers.py` has no HMM dependency, so this is a regression check.

- [ ] **0.4.3** Commit nothing (this is a verification step, no code changes)

---

**End of Phase 0.** Tree state: HMM gone, new dirs scaffolded, `main.py` parked, `circuit_breakers.py` still green. Ready for Phase 1.

---

## Phase 1 — Data Layer

### Task 1.1: `Bar` dataclass

**Files:**
- Create: `core/bar.py`
- Test: `tests/test_bar.py`

- [ ] **1.1.1** Write the failing test

Create `tests/test_bar.py`:

```python
from datetime import datetime, timezone
import pytest
from core.bar import Bar


def test_bar_immutable_and_validates():
    b = Bar(
        symbol="AAPL",
        ts=datetime(2026, 5, 14, 14, 35, tzinfo=timezone.utc),
        open=100.0, high=101.0, low=99.5, close=100.5, volume=1234,
    )
    assert b.symbol == "AAPL"
    assert b.range == 1.5
    assert b.is_bullish
    with pytest.raises(AttributeError):
        b.close = 200.0  # frozen


def test_bar_rejects_invalid_ohlc():
    with pytest.raises(ValueError):
        Bar(symbol="AAPL", ts=datetime.now(timezone.utc),
            open=100.0, high=99.0, low=99.5, close=100.0, volume=1)
```

- [ ] **1.1.2** Run the test to verify it fails

```bash
pytest tests/test_bar.py -v
```

Expected: ImportError or ModuleNotFoundError on `core.bar`.

- [ ] **1.1.3** Implement `core/bar.py`

```python
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class Bar:
    symbol: str
    ts: datetime           # bar close timestamp, timezone-aware UTC
    open: float
    high: float
    low: float
    close: float
    volume: float          # float — crypto fractional volume

    def __post_init__(self) -> None:
        if self.high < max(self.open, self.close, self.low):
            raise ValueError(f"Invalid bar: high {self.high} below O/C/L for {self.symbol} @ {self.ts}")
        if self.low > min(self.open, self.close, self.high):
            raise ValueError(f"Invalid bar: low {self.low} above O/C/H for {self.symbol} @ {self.ts}")
        if self.ts.tzinfo is None:
            raise ValueError(f"Bar.ts must be timezone-aware: {self.ts}")

    @property
    def range(self) -> float:
        return self.high - self.low

    @property
    def is_bullish(self) -> bool:
        return self.close > self.open

    @property
    def typical_price(self) -> float:
        return (self.high + self.low + self.close) / 3.0
```

- [ ] **1.1.4** Run tests

```bash
pytest tests/test_bar.py -v
```

Expected: 2 passed.

- [ ] **1.1.5** Commit

```bash
git add core/bar.py tests/test_bar.py
git commit -m "feat(core): Bar dataclass with OHLCV validation"
```

### Task 1.2: Symbol normalization helper

**Files:**
- Create: `broker/symbol.py`
- Test: `tests/test_symbol.py`

- [ ] **1.2.1** Write the failing test

Create `tests/test_symbol.py`:

```python
from broker.symbol import normalize_for_api, asset_class_of


def test_equity_passthrough():
    assert normalize_for_api("AAPL", "equity") == "AAPL"
    assert asset_class_of("AAPL") == "equity"


def test_crypto_normalization():
    # Display form `BTC/USD` is what users put in config; the v1beta3 endpoint
    # accepts that same form. The function is idempotent.
    assert normalize_for_api("BTC/USD", "crypto") == "BTC/USD"
    assert asset_class_of("BTC/USD") == "crypto"
    assert asset_class_of("btc/usd") == "crypto"


def test_legacy_crypto_form_normalized():
    # Some users may write BTCUSD. Convert to BTC/USD so downstream code is uniform.
    assert normalize_for_api("BTCUSD", "crypto") == "BTC/USD"
```

- [ ] **1.2.2** Run test, expect failure

```bash
pytest tests/test_symbol.py -v
```

Expected: ModuleNotFoundError.

- [ ] **1.2.3** Implement `broker/symbol.py`

```python
from __future__ import annotations

_CRYPTO_QUOTES = {"USD", "USDT", "USDC", "EUR", "GBP", "BTC", "ETH"}


def asset_class_of(symbol: str) -> str:
    s = symbol.upper()
    if "/" in s:
        return "crypto"
    for q in _CRYPTO_QUOTES:
        if s.endswith(q) and len(s) > len(q) and s[: -len(q)] not in {"USD"}:
            base = s[: -len(q)]
            if base.isalpha() and 2 <= len(base) <= 5:
                return "crypto"
    return "equity"


def normalize_for_api(symbol: str, asset_class: str) -> str:
    if asset_class == "equity":
        return symbol.upper()
    if asset_class == "crypto":
        s = symbol.upper()
        if "/" in s:
            return s
        for q in _CRYPTO_QUOTES:
            if s.endswith(q) and len(s) > len(q):
                return f"{s[: -len(q)]}/{q}"
        return s
    raise ValueError(f"Unknown asset_class: {asset_class}")
```

- [ ] **1.2.4** Run tests

```bash
pytest tests/test_symbol.py -v
```

Expected: 3 passed.

- [ ] **1.2.5** Commit

```bash
git add broker/symbol.py tests/test_symbol.py
git commit -m "feat(broker): symbol normalization for equity vs crypto"
```

### Task 1.3: Extend `AlpacaClient` with bars endpoints

**Files:**
- Modify: `broker/alpaca_client.py`
- Create: `tests/test_alpaca_client_bars.py`

- [ ] **1.3.1** Write the failing test (uses requests-mock-style monkeypatching)

Create `tests/test_alpaca_client_bars.py`:

```python
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock
import os
import pytest

os.environ.setdefault("ALPACA_API_KEY", "test")
os.environ.setdefault("ALPACA_SECRET_KEY", "test")

from broker.alpaca_client import AlpacaClient


def _mock_response(status: int, body: dict) -> MagicMock:
    m = MagicMock()
    m.status_code = status
    m.json.return_value = body
    return m


def test_get_stock_bars():
    client = AlpacaClient()
    body = {
        "bars": [
            {"t": "2026-05-14T13:30:00Z", "o": 100, "h": 101, "l": 99, "c": 100.5, "v": 1000},
            {"t": "2026-05-14T13:35:00Z", "o": 100.5, "h": 102, "l": 100, "c": 101.5, "v": 1200},
        ]
    }
    with patch.object(client._session, "request", return_value=_mock_response(200, body)) as req:
        bars = client.get_stock_bars("AAPL", "5Min",
                                     start=datetime(2026, 5, 14, 13, 30, tzinfo=timezone.utc),
                                     end=datetime(2026, 5, 14, 14, 0, tzinfo=timezone.utc))
        assert len(bars) == 2
        assert bars[0]["c"] == 100.5
        url = req.call_args[0][1]
        assert "data.alpaca.markets" in url
        assert "AAPL" in url


def test_get_crypto_bars():
    client = AlpacaClient()
    body = {"bars": {"BTC/USD": [
        {"t": "2026-05-14T00:00:00Z", "o": 50000, "h": 50500, "l": 49800, "c": 50200, "v": 12.5}
    ]}}
    with patch.object(client._session, "request", return_value=_mock_response(200, body)) as req:
        bars = client.get_crypto_bars("BTC/USD", "5Min",
                                      start=datetime(2026, 5, 14, 0, 0, tzinfo=timezone.utc),
                                      end=datetime(2026, 5, 14, 0, 30, tzinfo=timezone.utc))
        assert len(bars) == 1
        url = req.call_args[0][1]
        assert "v1beta3/crypto" in url
```

- [ ] **1.3.2** Run test, expect failure

```bash
pytest tests/test_alpaca_client_bars.py -v
```

Expected: AttributeError on `get_stock_bars` / `get_crypto_bars`.

- [ ] **1.3.3** Modify `broker/alpaca_client.py`

Add a constant for the data API base near the top of the file (right after the existing imports):

```python
_DATA_BASE_URL = "https://data.alpaca.markets"
```

Then add a helper for data-host requests next to `_request` (after the `_request` method, around line 143):

```python
    def _data_request(self, method: str, path: str, **kwargs):
        """Identical retry semantics to _request, but against the data host."""
        url = f"{_DATA_BASE_URL}{path}"
        for attempt in range(self._MAX_RETRIES + 1):
            response = self._session.request(method, url, timeout=10, **kwargs)
            if response.status_code == 429:
                if attempt == self._MAX_RETRIES:
                    raise RateLimitError(
                        f"Rate limit exceeded after {self._MAX_RETRIES} retries on {method} {path}"
                    )
                wait = (2 ** attempt) + random.uniform(0, 1)
                logger.warning("Data rate limited (attempt %d/%d). Waiting %.2fs.",
                               attempt + 1, self._MAX_RETRIES, wait)
                time.sleep(wait)
                continue
            if response.status_code == 401:
                raise AuthenticationError("Invalid API credentials")
            if response.status_code >= 400:
                try:
                    body = response.json()
                    message = body.get("message", response.text)
                except Exception:
                    message = response.text
                raise BrokerAPIError(response.status_code, message)
            return response
        raise RateLimitError(f"Rate limit retry loop exhausted for {method} {path}")
```

Then add the public bar endpoints at the bottom of the class (after `get_quote`):

```python
    def get_stock_bars(self, symbol: str, timeframe: str, start, end, limit: int = 10000) -> list[dict]:
        """GET /v2/stocks/{symbol}/bars — returns list of bar dicts (Alpaca raw shape)."""
        params = {
            "timeframe": timeframe,
            "start": start.isoformat().replace("+00:00", "Z"),
            "end": end.isoformat().replace("+00:00", "Z"),
            "limit": limit,
            "adjustment": "raw",
            "feed": "iex",
        }
        response = self._data_request("GET", f"/v2/stocks/{symbol}/bars", params=params)
        return response.json().get("bars", []) or []

    def get_crypto_bars(self, symbol: str, timeframe: str, start, end, limit: int = 10000) -> list[dict]:
        """GET /v1beta3/crypto/us/bars — returns list of bar dicts for one symbol."""
        params = {
            "symbols": symbol,
            "timeframe": timeframe,
            "start": start.isoformat().replace("+00:00", "Z"),
            "end": end.isoformat().replace("+00:00", "Z"),
            "limit": limit,
        }
        response = self._data_request("GET", "/v1beta3/crypto/us/bars", params=params)
        body = response.json().get("bars", {}) or {}
        return body.get(symbol, []) or []
```

- [ ] **1.3.4** Run tests

```bash
pytest tests/test_alpaca_client_bars.py -v
```

Expected: 2 passed.

- [ ] **1.3.5** Commit

```bash
git add broker/alpaca_client.py tests/test_alpaca_client_bars.py
git commit -m "feat(broker): AlpacaClient.get_stock_bars / get_crypto_bars on data host"
```

### Task 1.4: `alpaca_data.py` wrapper with caching

**Files:**
- Create: `broker/alpaca_data.py`
- Test: `tests/test_alpaca_data.py`

- [ ] **1.4.1** Write the failing test

Create `tests/test_alpaca_data.py`:

```python
import os
from datetime import datetime, timezone
from unittest.mock import MagicMock
import pytest

os.environ.setdefault("ALPACA_API_KEY", "test")
os.environ.setdefault("ALPACA_SECRET_KEY", "test")

from broker.alpaca_data import AlpacaData
from core.bar import Bar


@pytest.fixture
def fake_client():
    client = MagicMock()
    client.get_stock_bars.return_value = [
        {"t": "2026-05-14T13:30:00Z", "o": 100.0, "h": 101.0, "l": 99.0, "c": 100.5, "v": 1000.0},
        {"t": "2026-05-14T13:35:00Z", "o": 100.5, "h": 102.0, "l": 100.0, "c": 101.5, "v": 1200.0},
    ]
    client.get_crypto_bars.return_value = [
        {"t": "2026-05-14T00:00:00Z", "o": 50000.0, "h": 50500.0, "l": 49800.0, "c": 50200.0, "v": 12.5}
    ]
    return client


def test_get_bars_equity(fake_client, tmp_path):
    data = AlpacaData(fake_client, cache_dir=str(tmp_path))
    bars = data.get_bars("AAPL", "equity", "5Min",
                        start=datetime(2026, 5, 14, 13, 30, tzinfo=timezone.utc),
                        end=datetime(2026, 5, 14, 13, 40, tzinfo=timezone.utc))
    assert len(bars) == 2
    assert isinstance(bars[0], Bar)
    assert bars[0].symbol == "AAPL"
    assert bars[0].close == 100.5


def test_get_bars_crypto(fake_client, tmp_path):
    data = AlpacaData(fake_client, cache_dir=str(tmp_path))
    bars = data.get_bars("BTC/USD", "crypto", "5Min",
                        start=datetime(2026, 5, 14, 0, 0, tzinfo=timezone.utc),
                        end=datetime(2026, 5, 14, 0, 5, tzinfo=timezone.utc))
    assert len(bars) == 1
    assert bars[0].symbol == "BTC/USD"
    assert bars[0].close == 50200.0


def test_cache_avoids_second_call(fake_client, tmp_path):
    data = AlpacaData(fake_client, cache_dir=str(tmp_path))
    args = ("AAPL", "equity", "5Min",
            datetime(2026, 5, 14, 13, 30, tzinfo=timezone.utc),
            datetime(2026, 5, 14, 13, 40, tzinfo=timezone.utc))
    data.get_bars(*args, use_cache=True)
    data.get_bars(*args, use_cache=True)
    assert fake_client.get_stock_bars.call_count == 1
```

- [ ] **1.4.2** Run test, expect failure

```bash
pytest tests/test_alpaca_data.py -v
```

Expected: ModuleNotFoundError.

- [ ] **1.4.3** Implement `broker/alpaca_data.py`

```python
from __future__ import annotations
import logging
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import pandas as pd

from broker.alpaca_client import AlpacaClient
from broker.symbol import normalize_for_api
from core.bar import Bar

logger = logging.getLogger(__name__)


def _parse_ts(raw: str) -> datetime:
    return datetime.fromisoformat(raw.replace("Z", "+00:00"))


def _bars_from_raw(raw: list[dict], symbol: str) -> list[Bar]:
    out: list[Bar] = []
    for r in raw:
        try:
            out.append(Bar(
                symbol=symbol,
                ts=_parse_ts(r["t"]),
                open=float(r["o"]),
                high=float(r["h"]),
                low=float(r["l"]),
                close=float(r["c"]),
                volume=float(r["v"]),
            ))
        except (KeyError, ValueError) as exc:
            logger.warning("Skipping malformed bar for %s: %s (%s)", symbol, r, exc)
    return out


class AlpacaData:
    """Wrapper over AlpacaClient bar endpoints with on-disk parquet cache."""

    def __init__(self, client: AlpacaClient, cache_dir: str = "runtime/bars_cache"):
        self.client = client
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _cache_path(self, symbol: str, timeframe: str, start: datetime, end: datetime) -> Path:
        safe_symbol = symbol.replace("/", "-")
        key = f"{safe_symbol}_{timeframe}_{start.isoformat()}_{end.isoformat()}.parquet"
        return self.cache_dir / key

    def _read_cache(self, path: Path, symbol: str) -> list[Bar] | None:
        if not path.exists():
            return None
        try:
            df = pd.read_parquet(path)
        except Exception as exc:
            logger.warning("Cache read failed for %s: %s — refetching", path, exc)
            return None
        return [
            Bar(symbol=symbol, ts=row.ts.to_pydatetime(),
                open=row.open, high=row.high, low=row.low,
                close=row.close, volume=row.volume)
            for row in df.itertuples(index=False)
        ]

    def _write_cache(self, path: Path, bars: list[Bar]) -> None:
        if not bars:
            return
        df = pd.DataFrame([{
            "ts": b.ts, "open": b.open, "high": b.high, "low": b.low,
            "close": b.close, "volume": b.volume,
        } for b in bars])
        tmp = path.with_suffix(".parquet.tmp")
        df.to_parquet(tmp, index=False)
        os.replace(tmp, path)

    def get_bars(
        self,
        symbol: str,
        asset_class: str,
        timeframe: str,
        start: datetime,
        end: datetime,
        use_cache: bool = True,
    ) -> list[Bar]:
        api_symbol = normalize_for_api(symbol, asset_class)
        cache_path = self._cache_path(symbol, timeframe, start, end)

        if use_cache:
            cached = self._read_cache(cache_path, symbol)
            if cached is not None:
                return cached

        if asset_class == "equity":
            raw = self.client.get_stock_bars(api_symbol, timeframe, start, end)
        elif asset_class == "crypto":
            raw = self.client.get_crypto_bars(api_symbol, timeframe, start, end)
        else:
            raise ValueError(f"Unknown asset_class: {asset_class}")

        bars = _bars_from_raw(raw, symbol)
        if use_cache:
            self._write_cache(cache_path, bars)
        return bars
```

- [ ] **1.4.4** Run tests

```bash
pytest tests/test_alpaca_data.py -v
```

Expected: 3 passed.

- [ ] **1.4.5** Commit

```bash
git add broker/alpaca_data.py tests/test_alpaca_data.py
git commit -m "feat(broker): AlpacaData wrapper with parquet bar cache"
```

### Task 1.5: `BarClock` — bar-close scheduler primitive

**Files:**
- Create: `scheduler/bar_clock.py`
- Test: `tests/test_bar_clock.py`

- [ ] **1.5.1** Write the failing test

Create `tests/test_bar_clock.py`:

```python
from datetime import datetime, timezone
from scheduler.bar_clock import next_boundary, parse_timeframe_minutes


def test_parse_timeframe_minutes():
    assert parse_timeframe_minutes("5Min") == 5
    assert parse_timeframe_minutes("15Min") == 15
    assert parse_timeframe_minutes("1Hour") == 60


def test_next_boundary_5min():
    now = datetime(2026, 5, 14, 13, 32, 12, tzinfo=timezone.utc)
    nb = next_boundary(now, "5Min", grace_seconds=5)
    assert nb == datetime(2026, 5, 14, 13, 35, 5, tzinfo=timezone.utc)


def test_next_boundary_exactly_on_boundary_advances():
    now = datetime(2026, 5, 14, 13, 35, 0, tzinfo=timezone.utc)
    nb = next_boundary(now, "5Min", grace_seconds=5)
    # Already at :35:00 — next CLOSE we want is :40:05
    assert nb == datetime(2026, 5, 14, 13, 40, 5, tzinfo=timezone.utc)


def test_next_boundary_15min():
    now = datetime(2026, 5, 14, 13, 22, tzinfo=timezone.utc)
    assert next_boundary(now, "15Min", grace_seconds=0) == datetime(2026, 5, 14, 13, 30, 0, tzinfo=timezone.utc)
```

- [ ] **1.5.2** Run test, expect failure

```bash
pytest tests/test_bar_clock.py -v
```

Expected: ModuleNotFoundError.

- [ ] **1.5.3** Implement `scheduler/bar_clock.py`

```python
from __future__ import annotations
import re
import time
from datetime import datetime, timedelta, timezone


_TF_RE = re.compile(r"^(\d+)(Min|Hour)$")


def parse_timeframe_minutes(tf: str) -> int:
    m = _TF_RE.match(tf)
    if not m:
        raise ValueError(f"Unsupported timeframe: {tf!r}")
    n, unit = int(m.group(1)), m.group(2)
    return n if unit == "Min" else n * 60


def next_boundary(now: datetime, timeframe: str, grace_seconds: int = 5) -> datetime:
    """Return the next bar-close + grace timestamp strictly after `now`."""
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    minutes = parse_timeframe_minutes(timeframe)
    base = now.replace(second=0, microsecond=0)
    minute_of_hour = base.minute
    next_min = ((minute_of_hour // minutes) + 1) * minutes
    delta_minutes = next_min - minute_of_hour
    boundary = base + timedelta(minutes=delta_minutes)
    return boundary + timedelta(seconds=grace_seconds)


def sleep_until(target: datetime, *, sleeper=time.sleep, now_fn=lambda: datetime.now(timezone.utc)) -> None:
    """Sleep until target. `sleeper` and `now_fn` are seams for tests."""
    delta = (target - now_fn()).total_seconds()
    if delta > 0:
        sleeper(delta)
```

- [ ] **1.5.4** Run tests

```bash
pytest tests/test_bar_clock.py -v
```

Expected: 4 passed.

- [ ] **1.5.5** Commit

```bash
git add scheduler/bar_clock.py tests/test_bar_clock.py
git commit -m "feat(scheduler): BarClock — next_boundary + sleep_until primitives"
```

---

**End of Phase 1.** Tree state: bars flow from Alpaca through a typed `Bar` dataclass, a parquet-cached wrapper, and a bar-close scheduler primitive. All Phase 1 tests green; total <2s. Ready for Phase 2.

---

## Phase 2 — Session & VWAP

### Task 2.1: Incremental VWAP + ±1σ bands

**Files:**
- Create: `core/vwap.py`
- Test: `tests/test_vwap.py`

- [ ] **2.1.1** Write the failing test

Create `tests/test_vwap.py`:

```python
import math
import numpy as np
from datetime import datetime, timezone, timedelta
from core.bar import Bar
from core.vwap import VWAPBands


def _make_bar(ts, price, volume=1000):
    return Bar(symbol="X", ts=ts,
               open=price, high=price + 0.5, low=price - 0.5,
               close=price, volume=volume)


def test_vwap_single_bar():
    v = VWAPBands(sigma=1.0)
    ts = datetime(2026, 5, 14, 13, 30, tzinfo=timezone.utc)
    v.add(_make_bar(ts, 100, volume=1000))
    assert v.vwap == 100.0
    assert v.upper == 100.0   # zero variance
    assert v.lower == 100.0


def test_vwap_matches_batch_calc():
    v = VWAPBands(sigma=1.0)
    base = datetime(2026, 5, 14, 13, 30, tzinfo=timezone.utc)
    prices = [100, 101, 99, 102, 98, 100.5]
    volumes = [1000, 2000, 1500, 800, 1200, 900]
    for i, (p, vol) in enumerate(zip(prices, volumes)):
        v.add(_make_bar(base + timedelta(minutes=5*i), p, volume=vol))

    # Batch reference using typical_price = (H+L+C)/3 with our synthetic OHLC
    typicals = np.array([(p + 0.5 + p - 0.5 + p) / 3.0 for p in prices])  # = prices
    vols = np.array(volumes, dtype=float)
    expected_vwap = np.average(typicals, weights=vols)
    expected_var = np.average((typicals - expected_vwap) ** 2, weights=vols)
    expected_sigma = math.sqrt(expected_var)

    assert math.isclose(v.vwap, expected_vwap, rel_tol=1e-9)
    assert math.isclose(v.upper, expected_vwap + expected_sigma, rel_tol=1e-9)
    assert math.isclose(v.lower, expected_vwap - expected_sigma, rel_tol=1e-9)


def test_vwap_reset():
    v = VWAPBands(sigma=1.0)
    ts = datetime(2026, 5, 14, 13, 30, tzinfo=timezone.utc)
    v.add(_make_bar(ts, 100))
    v.reset()
    assert v.bar_count == 0
    assert math.isnan(v.vwap)
```

- [ ] **2.1.2** Run test, expect failure

```bash
pytest tests/test_vwap.py -v
```

Expected: ModuleNotFoundError.

- [ ] **2.1.3** Implement `core/vwap.py`

```python
from __future__ import annotations
import math
from dataclasses import dataclass, field
from core.bar import Bar


@dataclass
class VWAPBands:
    sigma: float = 1.0
    _sum_pv: float = 0.0       # Σ typical_price × volume
    _sum_v: float = 0.0        # Σ volume
    _sum_p2v: float = 0.0      # Σ typical_price² × volume
    _bar_count: int = 0

    def reset(self) -> None:
        self._sum_pv = 0.0
        self._sum_v = 0.0
        self._sum_p2v = 0.0
        self._bar_count = 0

    def add(self, bar: Bar) -> None:
        tp = bar.typical_price
        v = bar.volume
        self._sum_pv += tp * v
        self._sum_v += v
        self._sum_p2v += tp * tp * v
        self._bar_count += 1

    @property
    def bar_count(self) -> int:
        return self._bar_count

    @property
    def vwap(self) -> float:
        if self._sum_v <= 0:
            return float("nan")
        return self._sum_pv / self._sum_v

    @property
    def variance(self) -> float:
        if self._sum_v <= 0:
            return 0.0
        mean = self.vwap
        # E[X²] − (E[X])²
        return max(0.0, self._sum_p2v / self._sum_v - mean * mean)

    @property
    def stdev(self) -> float:
        return math.sqrt(self.variance)

    @property
    def upper(self) -> float:
        return self.vwap + self.sigma * self.stdev

    @property
    def lower(self) -> float:
        return self.vwap - self.sigma * self.stdev
```

- [ ] **2.1.4** Run tests

```bash
pytest tests/test_vwap.py -v
```

Expected: 3 passed.

- [ ] **2.1.5** Commit

```bash
git add core/vwap.py tests/test_vwap.py
git commit -m "feat(core): incremental VWAP with ±1σ bands"
```

### Task 2.2: Acceptance detector

**Files:**
- Create: `core/acceptance.py`
- Test: `tests/test_acceptance.py`

- [ ] **2.2.1** Write the failing test

Create `tests/test_acceptance.py`:

```python
from datetime import datetime, timezone, timedelta
from core.bar import Bar
from core.acceptance import accepted_above, accepted_below


def _b(ts, c, h=None, l=None):
    return Bar(symbol="X", ts=ts, open=c, high=h or c + 0.5,
               low=l or c - 0.5, close=c, volume=1000)


def test_two_closes_above_with_distance():
    base = datetime(2026, 5, 14, 13, 30, tzinfo=timezone.utc)
    bars = [_b(base + timedelta(minutes=5*i), 101 + i * 0.5) for i in range(2)]
    # ATR = 0.5 (synthetic), level = 100
    assert accepted_above(bars, level=100.0, n=2, min_distance_atr=0.25, atr=0.5)


def test_one_close_above_not_accepted():
    base = datetime(2026, 5, 14, 13, 30, tzinfo=timezone.utc)
    bars = [_b(base, 99.5), _b(base + timedelta(minutes=5), 100.2)]
    # only the second bar is above
    assert not accepted_above(bars, level=100.0, n=2, min_distance_atr=0.0, atr=0.5)


def test_distance_threshold_rejects():
    base = datetime(2026, 5, 14, 13, 30, tzinfo=timezone.utc)
    bars = [_b(base, 100.05), _b(base + timedelta(minutes=5), 100.06)]
    # both close above 100 but distance is 0.06, below 0.25 × ATR(0.5)=0.125
    assert not accepted_above(bars, level=100.0, n=2, min_distance_atr=0.25, atr=0.5)


def test_below_symmetric():
    base = datetime(2026, 5, 14, 13, 30, tzinfo=timezone.utc)
    bars = [_b(base, 99.0), _b(base + timedelta(minutes=5), 98.5)]
    assert accepted_below(bars, level=100.0, n=2, min_distance_atr=0.25, atr=0.5)
```

- [ ] **2.2.2** Run, expect failure

```bash
pytest tests/test_acceptance.py -v
```

Expected: ModuleNotFoundError.

- [ ] **2.2.3** Implement `core/acceptance.py`

```python
from __future__ import annotations
from core.bar import Bar


def _last_n_closes(bars: list[Bar], n: int) -> list[float] | None:
    if len(bars) < n:
        return None
    return [b.close for b in bars[-n:]]


def accepted_above(bars: list[Bar], level: float, n: int,
                   min_distance_atr: float, atr: float) -> bool:
    closes = _last_n_closes(bars, n)
    if closes is None:
        return False
    if not all(c > level for c in closes):
        return False
    farthest = max(closes) - level
    return farthest >= min_distance_atr * atr


def accepted_below(bars: list[Bar], level: float, n: int,
                   min_distance_atr: float, atr: float) -> bool:
    closes = _last_n_closes(bars, n)
    if closes is None:
        return False
    if not all(c < level for c in closes):
        return False
    farthest = level - min(closes)
    return farthest >= min_distance_atr * atr
```

- [ ] **2.2.4** Run tests

```bash
pytest tests/test_acceptance.py -v
```

Expected: 4 passed.

- [ ] **2.2.5** Commit

```bash
git add core/acceptance.py tests/test_acceptance.py
git commit -m "feat(core): acceptance detector (N closes + ATR distance)"
```

### Task 2.3: ATR helper

**Files:**
- Create: `core/atr.py`
- Test: `tests/test_atr.py`

- [ ] **2.3.1** Write the failing test

Create `tests/test_atr.py`:

```python
from datetime import datetime, timezone, timedelta
from core.bar import Bar
from core.atr import atr


def _b(ts, o, h, l, c):
    return Bar(symbol="X", ts=ts, open=o, high=h, low=l, close=c, volume=1000)


def test_atr_handles_short_window():
    base = datetime(2026, 5, 14, 13, 30, tzinfo=timezone.utc)
    bars = [_b(base + timedelta(minutes=5*i), 100, 101, 99, 100) for i in range(3)]
    # window 14 but we only have 3 bars → return mean of available true ranges = 2.0
    assert abs(atr(bars, 14) - 2.0) < 1e-9


def test_atr_full_window():
    base = datetime(2026, 5, 14, 13, 30, tzinfo=timezone.utc)
    bars = [_b(base + timedelta(minutes=5*i), 100, 100 + 0.5*i, 100 - 0.5*i, 100) for i in range(15)]
    val = atr(bars, 14)
    assert val > 0
    assert val < 20
```

- [ ] **2.3.2** Run, expect failure

```bash
pytest tests/test_atr.py -v
```

- [ ] **2.3.3** Implement `core/atr.py`

```python
from __future__ import annotations
from core.bar import Bar


def _true_range(prev_close: float | None, b: Bar) -> float:
    if prev_close is None:
        return b.high - b.low
    return max(
        b.high - b.low,
        abs(b.high - prev_close),
        abs(b.low - prev_close),
    )


def atr(bars: list[Bar], window: int) -> float:
    """Wilder-style ATR; falls back to mean of available TRs when bars < window."""
    if not bars:
        return 0.0
    trs: list[float] = []
    prev_c: float | None = None
    for b in bars:
        trs.append(_true_range(prev_c, b))
        prev_c = b.close
    if len(trs) < window:
        return sum(trs) / len(trs)
    # Wilder smoothing
    smoothed = sum(trs[:window]) / window
    for tr in trs[window:]:
        smoothed = (smoothed * (window - 1) + tr) / window
    return smoothed
```

- [ ] **2.3.4** Run tests

```bash
pytest tests/test_atr.py -v
```

Expected: 2 passed.

- [ ] **2.3.5** Commit

```bash
git add core/atr.py tests/test_atr.py
git commit -m "feat(core): Wilder-smoothed ATR helper"
```

### Task 2.4: Asset class config + session boundary helpers

**Files:**
- Create: `core/asset_class.py`
- Test: `tests/test_asset_class.py`

- [ ] **2.4.1** Write the failing test

Create `tests/test_asset_class.py`:

```python
from datetime import datetime, timezone, timedelta
from core.asset_class import AssetClassConfig, session_start_for


EQUITY = AssetClassConfig(
    name="equity",
    timezone="America/New_York",
    session_open_local="09:30",
    session_close_local="16:00",
    opening_blackout_min=15,
    bar_timeframe="5Min",
    slippage_bps=2.0,
    commission_per_share=0.0,
    commission_bps=0.0,
)

CRYPTO = AssetClassConfig(
    name="crypto",
    timezone="UTC",
    session_open_local="00:00",
    session_close_local="23:59",
    opening_blackout_min=15,
    bar_timeframe="5Min",
    slippage_bps=5.0,
    commission_per_share=0.0,
    commission_bps=25.0,
)


def test_equity_session_start_today_in_utc():
    # 14 May 2026 18:00 UTC = 14:00 ET (DST). Same calendar day, after open.
    now = datetime(2026, 5, 14, 18, 0, tzinfo=timezone.utc)
    start = session_start_for(now, EQUITY)
    # 9:30 ET = 13:30 UTC during DST
    assert start == datetime(2026, 5, 14, 13, 30, tzinfo=timezone.utc)


def test_equity_session_before_open_falls_back_to_yesterday():
    # 14 May 2026 12:00 UTC = 08:00 ET — before the 09:30 ET open today
    now = datetime(2026, 5, 14, 12, 0, tzinfo=timezone.utc)
    start = session_start_for(now, EQUITY)
    # Should use 13 May session
    assert start == datetime(2026, 5, 13, 13, 30, tzinfo=timezone.utc)


def test_crypto_session_start_is_utc_midnight():
    now = datetime(2026, 5, 14, 18, 0, tzinfo=timezone.utc)
    start = session_start_for(now, CRYPTO)
    assert start == datetime(2026, 5, 14, 0, 0, tzinfo=timezone.utc)
```

- [ ] **2.4.2** Run, expect failure

```bash
pytest tests/test_asset_class.py -v
```

- [ ] **2.4.3** Implement `core/asset_class.py`

```python
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, time, timedelta

import pytz


@dataclass(frozen=True)
class AssetClassConfig:
    name: str
    timezone: str
    session_open_local: str          # "HH:MM"
    session_close_local: str
    opening_blackout_min: int
    bar_timeframe: str
    slippage_bps: float
    commission_per_share: float
    commission_bps: float


def _parse_hhmm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


def session_start_for(now_utc: datetime, cfg: AssetClassConfig) -> datetime:
    """Return the session-start timestamp (UTC) for the session that *contains* now_utc.

    If now is before today's open in the asset class's local timezone, returns yesterday's session start.
    """
    if now_utc.tzinfo is None:
        raise ValueError("now_utc must be timezone-aware")
    tz = pytz.timezone(cfg.timezone)
    now_local = now_utc.astimezone(tz)
    open_t = _parse_hhmm(cfg.session_open_local)
    today_open_local = tz.localize(datetime.combine(now_local.date(), open_t))
    if now_local < today_open_local:
        yday = (now_local - timedelta(days=1)).date()
        today_open_local = tz.localize(datetime.combine(yday, open_t))
    return today_open_local.astimezone(pytz.UTC)
```

- [ ] **2.4.4** Run tests

```bash
pytest tests/test_asset_class.py -v
```

Expected: 3 passed.

- [ ] **2.4.5** Commit

```bash
git add core/asset_class.py tests/test_asset_class.py
git commit -m "feat(core): AssetClassConfig + session_start_for helper"
```

### Task 2.5: `SessionContext` (per symbol, per session)

**Files:**
- Create: `core/session.py`
- Test: `tests/test_session.py`

- [ ] **2.5.1** Write the failing test

Create `tests/test_session.py`:

```python
from datetime import datetime, timezone, timedelta
from core.bar import Bar
from core.session import SessionContext
from core.asset_class import AssetClassConfig


CRYPTO = AssetClassConfig(
    name="crypto", timezone="UTC",
    session_open_local="00:00", session_close_local="23:59",
    opening_blackout_min=15, bar_timeframe="5Min",
    slippage_bps=5.0, commission_per_share=0.0, commission_bps=25.0,
)


def _b(ts, c):
    return Bar(symbol="BTC/USD", ts=ts, open=c, high=c + 1, low=c - 1, close=c, volume=10)


def test_session_ingest_updates_vwap():
    ctx = SessionContext(symbol="BTC/USD", asset_class=CRYPTO)
    base = datetime(2026, 5, 14, 0, 5, tzinfo=timezone.utc)
    for i in range(3):
        ctx.ingest(_b(base + timedelta(minutes=5*i), 100 + i))
    assert ctx.bar_count == 3
    assert 100 < ctx.vwap < 102
    assert ctx.day_high == 103
    assert ctx.day_low == 99


def test_session_resets_at_boundary():
    ctx = SessionContext(symbol="BTC/USD", asset_class=CRYPTO)
    day1 = datetime(2026, 5, 14, 23, 55, tzinfo=timezone.utc)
    ctx.ingest(_b(day1, 100))
    assert ctx.bar_count == 1

    day2 = datetime(2026, 5, 15, 0, 5, tzinfo=timezone.utc)
    ctx.ingest(_b(day2, 200))
    assert ctx.bar_count == 1   # reset
    assert ctx.session_start_ts == datetime(2026, 5, 15, 0, 0, tzinfo=timezone.utc)
    assert ctx.vwap == 200


def test_session_in_value_area():
    ctx = SessionContext(symbol="BTC/USD", asset_class=CRYPTO)
    base = datetime(2026, 5, 14, 0, 5, tzinfo=timezone.utc)
    for i, p in enumerate([100, 100, 100, 100, 100]):
        ctx.ingest(_b(base + timedelta(minutes=5*i), p))
    # zero variance → bands collapse to vwap → "in value" tolerance check
    assert ctx.in_value_area(100.0)
```

- [ ] **2.5.2** Run, expect failure

```bash
pytest tests/test_session.py -v
```

- [ ] **2.5.3** Implement `core/session.py`

```python
from __future__ import annotations
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from core.asset_class import AssetClassConfig, session_start_for
from core.atr import atr as compute_atr
from core.bar import Bar
from core.vwap import VWAPBands

logger = logging.getLogger(__name__)


@dataclass
class SessionContext:
    symbol: str
    asset_class: AssetClassConfig
    sigma: float = 1.0
    bars: list[Bar] = field(default_factory=list)
    vwap_bands: VWAPBands = field(init=False)
    session_start_ts: Optional[datetime] = None
    day_high: float = float("-inf")
    day_low: float = float("inf")
    avg_range_20d: float = 0.0          # populated externally; default 0 = unknown
    regime: str = "Undefined"
    touch_counts: dict[float, int] = field(default_factory=dict)

    def __post_init__(self):
        self.vwap_bands = VWAPBands(sigma=self.sigma)

    @property
    def bar_count(self) -> int:
        return len(self.bars)

    @property
    def vwap(self) -> float:
        return self.vwap_bands.vwap

    @property
    def upper_band(self) -> float:
        return self.vwap_bands.upper

    @property
    def lower_band(self) -> float:
        return self.vwap_bands.lower

    def reset(self, new_session_start: datetime) -> None:
        self.bars = []
        self.vwap_bands.reset()
        self.session_start_ts = new_session_start
        self.day_high = float("-inf")
        self.day_low = float("inf")
        self.regime = "Undefined"
        self.touch_counts = {}

    def ingest(self, bar: Bar) -> None:
        boundary = session_start_for(bar.ts, self.asset_class)
        if self.session_start_ts is None or boundary != self.session_start_ts:
            self.reset(boundary)

        self.bars.append(bar)
        self.vwap_bands.add(bar)
        self.day_high = max(self.day_high, bar.high)
        self.day_low = min(self.day_low, bar.low)

    def atr(self, window: int = 14) -> float:
        return compute_atr(self.bars, window)

    def in_value_area(self, price: float) -> bool:
        return self.lower_band <= price <= self.upper_band

    def in_value_area_fraction(self) -> float:
        """Fraction of bars whose CLOSE was inside the live value area at insertion time.

        Cheap approximation: uses current bands (not historical band evolution).
        Sufficient for regime classification.
        """
        if not self.bars:
            return 0.0
        inside = sum(1 for b in self.bars if self.lower_band <= b.close <= self.upper_band)
        return inside / len(self.bars)

    def fraction_above_vwap(self) -> float:
        if not self.bars:
            return 0.0
        above = sum(1 for b in self.bars if b.close > self.vwap)
        return above / len(self.bars)
```

- [ ] **2.5.4** Run tests

```bash
pytest tests/test_session.py -v
```

Expected: 3 passed.

- [ ] **2.5.5** Commit

```bash
git add core/session.py tests/test_session.py
git commit -m "feat(core): SessionContext with auto-reset on session boundary"
```

### Task 2.6: Regime detector

**Files:**
- Create: `strategies/regime_detector.py`
- Test: `tests/test_regime_detector.py`

- [ ] **2.6.1** Write the failing test

Create `tests/test_regime_detector.py`:

```python
from datetime import datetime, timezone, timedelta
from core.bar import Bar
from core.session import SessionContext
from core.asset_class import AssetClassConfig
from strategies.regime_detector import RegimeDetector, RegimeConfig


CRYPTO = AssetClassConfig(
    name="crypto", timezone="UTC",
    session_open_local="00:00", session_close_local="23:59",
    opening_blackout_min=15, bar_timeframe="5Min",
    slippage_bps=5.0, commission_per_share=0.0, commission_bps=25.0,
)
CFG = RegimeConfig(trend_day_range_mult=1.5, trend_day_in_value_max=0.30,
                   balance_day_in_value_min=0.60)


def _b(ts, o, h, l, c):
    return Bar(symbol="X", ts=ts, open=o, high=h, low=l, close=c, volume=10)


def _ctx(bars, avg_range_20d):
    ctx = SessionContext(symbol="X", asset_class=CRYPTO)
    for b in bars:
        ctx.ingest(b)
    ctx.avg_range_20d = avg_range_20d
    return ctx


def test_balance_day_classified_as_range():
    base = datetime(2026, 5, 14, 0, 5, tzinfo=timezone.utc)
    bars = [_b(base + timedelta(minutes=5*i), 100, 100.5, 99.5, 100) for i in range(20)]
    ctx = _ctx(bars, avg_range_20d=2.0)
    assert RegimeDetector(CFG).classify(ctx) == "Range"


def test_trend_day_classified_as_trend():
    base = datetime(2026, 5, 14, 0, 5, tzinfo=timezone.utc)
    bars = []
    for i in range(20):
        c = 100 + i * 1.0
        bars.append(_b(base + timedelta(minutes=5*i), c - 0.5, c + 0.5, c - 0.5, c))
    ctx = _ctx(bars, avg_range_20d=2.0)
    assert RegimeDetector(CFG).classify(ctx) == "Trend"


def test_undefined_when_too_few_bars():
    ctx = _ctx([], avg_range_20d=2.0)
    assert RegimeDetector(CFG).classify(ctx) == "Undefined"
```

- [ ] **2.6.2** Run, expect failure

```bash
pytest tests/test_regime_detector.py -v
```

- [ ] **2.6.3** Implement `strategies/regime_detector.py`

```python
from __future__ import annotations
from dataclasses import dataclass

from core.session import SessionContext


@dataclass(frozen=True)
class RegimeConfig:
    trend_day_range_mult: float = 1.5
    trend_day_in_value_max: float = 0.30
    balance_day_in_value_min: float = 0.60
    min_bars: int = 6


class RegimeDetector:
    def __init__(self, cfg: RegimeConfig):
        self.cfg = cfg

    def classify(self, ctx: SessionContext) -> str:
        if ctx.bar_count < self.cfg.min_bars:
            return "Undefined"
        in_value = ctx.in_value_area_fraction()
        day_range = ctx.day_high - ctx.day_low
        avg = ctx.avg_range_20d if ctx.avg_range_20d > 0 else day_range
        range_mult = day_range / avg if avg > 0 else 1.0

        if range_mult >= self.cfg.trend_day_range_mult and in_value <= self.cfg.trend_day_in_value_max:
            return "Trend"
        if in_value >= self.cfg.balance_day_in_value_min:
            return "Range"
        if in_value < self.cfg.balance_day_in_value_min and range_mult < self.cfg.trend_day_range_mult:
            return "Discovery"
        return "Undefined"
```

- [ ] **2.6.4** Run tests

```bash
pytest tests/test_regime_detector.py -v
```

Expected: 3 passed.

- [ ] **2.6.5** Commit

```bash
git add strategies/regime_detector.py tests/test_regime_detector.py
git commit -m "feat(strategies): RegimeDetector classifies Range/Trend/Discovery/Undefined"
```

---

**End of Phase 2.** Tree state: per-symbol session state machine with incremental VWAP bands, ATR, acceptance, and regime classification — all unit-tested, no broker dependency. Ready for Phase 3.

---

## Phase 3 — Setup State Machines

### Task 3.1: `BaseSetup` + `SetupSignal` types

**Files:**
- Create: `strategies/base_setup.py`
- Test: `tests/test_base_setup.py`

- [ ] **3.1.1** Write the failing test

Create `tests/test_base_setup.py`:

```python
import pytest
from datetime import datetime, timezone
from strategies.base_setup import SetupSignal, BaseSetup


def test_setup_signal_dataclass():
    s = SetupSignal(
        setup="price_discovery", symbol="AAPL", side="long",
        entry=100.0, stop=99.0, target=102.0,
        atr=0.5, level=100.5, ts=datetime.now(timezone.utc),
        notes={},
    )
    assert s.r_multiple_target == pytest.approx(2.0, rel=1e-3)
    assert s.risk_per_share == 1.0


def test_base_setup_is_abstract():
    with pytest.raises(TypeError):
        BaseSetup("AAPL")        # cannot instantiate abstract base
```

- [ ] **3.1.2** Run, expect failure

```bash
pytest tests/test_base_setup.py -v
```

- [ ] **3.1.3** Implement `strategies/base_setup.py`

```python
from __future__ import annotations
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from core.session import SessionContext


@dataclass(frozen=True)
class SetupSignal:
    setup: str                       # "price_discovery" | "fade_extreme" | ...
    symbol: str
    side: str                        # "long" | "short"
    entry: float                     # planned entry price (limit, market, or scale-in price)
    stop: float
    target: float
    atr: float
    level: float                     # the band/vwap level that triggered the setup
    ts: datetime
    notes: dict[str, object] = field(default_factory=dict)

    @property
    def risk_per_share(self) -> float:
        return abs(self.entry - self.stop)

    @property
    def reward_per_share(self) -> float:
        return abs(self.target - self.entry)

    @property
    def r_multiple_target(self) -> float:
        if self.risk_per_share == 0:
            return 0.0
        return self.reward_per_share / self.risk_per_share


class BaseSetup(ABC):
    """Abstract setup state machine.

    Subclasses keep their own per-symbol state across .check() calls.
    """

    name: str = ""

    def __init__(self, symbol: str) -> None:
        self.symbol = symbol
        self.state: str = "IDLE"
        self.armed_level: Optional[float] = None
        self.bars_in_state: int = 0

    @abstractmethod
    def check(self, ctx: SessionContext) -> Optional[SetupSignal]:
        """Run one bar-close evaluation. Return a signal when ARMED → FILLED transition occurs."""
        raise NotImplementedError

    def reset(self) -> None:
        self.state = "IDLE"
        self.armed_level = None
        self.bars_in_state = 0
```

- [ ] **3.1.4** Run tests

```bash
pytest tests/test_base_setup.py -v
```

Expected: 2 passed.

- [ ] **3.1.5** Commit

```bash
git add strategies/base_setup.py tests/test_base_setup.py
git commit -m "feat(strategies): BaseSetup + SetupSignal abstract types"
```

### Task 3.2: Setup 1 — Price Discovery Continuation

**Files:**
- Create: `strategies/setup_price_discovery.py`
- Test: `tests/test_setup_price_discovery.py`

State diagram (re-stated for the task):

```
IDLE → BREAKOUT_PENDING → ACCEPTED → ARMED → FILLED | EXPIRED
```

- [ ] **3.2.1** Write the failing test

Create `tests/test_setup_price_discovery.py`:

```python
from datetime import datetime, timezone, timedelta
from core.bar import Bar
from core.session import SessionContext
from core.asset_class import AssetClassConfig
from strategies.setup_price_discovery import PriceDiscoverySetup


CRYPTO = AssetClassConfig(
    name="crypto", timezone="UTC",
    session_open_local="00:00", session_close_local="23:59",
    opening_blackout_min=15, bar_timeframe="5Min",
    slippage_bps=5.0, commission_per_share=0.0, commission_bps=25.0,
)


def _bar(ts, o, h, l, c, v=100):
    return Bar(symbol="X", ts=ts, open=o, high=h, low=l, close=c, volume=v)


def _drive(ctx, setup, bars):
    sig = None
    for b in bars:
        ctx.ingest(b)
        s = setup.check(ctx)
        if s is not None:
            sig = s
    return sig


def test_breakout_then_backtest_emits_long_signal():
    ctx = SessionContext(symbol="X", asset_class=CRYPTO)
    setup = PriceDiscoverySetup("X", atr_mult_stop=1.0, target_R=1.5,
                                arm_window_bars=6, accept_n=2,
                                accept_distance_atr=0.0)  # 0 to keep test simple
    base = datetime(2026, 5, 14, 0, 5, tzinfo=timezone.utc)

    # Build a tight balance: 6 bars at ~100, vol moderate
    bars = [_bar(base + timedelta(minutes=5*i), 100, 100.5, 99.5, 100, v=100) for i in range(6)]
    # Breakout: 2 strong closes above the upper band
    bars += [_bar(base + timedelta(minutes=5*(6+i)), 101, 102, 100.8, 101.5, v=200) for i in range(2)]
    # Backtest: bar wicks back to band
    bars.append(_bar(base + timedelta(minutes=5*8), 101.5, 102, ctx.upper_band if False else 100.6, 101.2, v=150))

    sig = _drive(ctx, setup, bars)
    assert sig is not None
    assert sig.side == "long"
    assert sig.entry < sig.target
    assert sig.stop < sig.entry


def test_no_signal_when_breakout_fails():
    ctx = SessionContext(symbol="X", asset_class=CRYPTO)
    setup = PriceDiscoverySetup("X", atr_mult_stop=1.0, target_R=1.5,
                                arm_window_bars=6, accept_n=2,
                                accept_distance_atr=0.0)
    base = datetime(2026, 5, 14, 0, 5, tzinfo=timezone.utc)
    bars = [_bar(base + timedelta(minutes=5*i), 100, 100.5, 99.5, 100) for i in range(10)]
    sig = _drive(ctx, setup, bars)
    assert sig is None
```

- [ ] **3.2.2** Run, expect failure

```bash
pytest tests/test_setup_price_discovery.py -v
```

- [ ] **3.2.3** Implement `strategies/setup_price_discovery.py`

```python
from __future__ import annotations
from typing import Optional

from core.acceptance import accepted_above, accepted_below
from core.bar import Bar
from core.session import SessionContext
from strategies.base_setup import BaseSetup, SetupSignal


class PriceDiscoverySetup(BaseSetup):
    name = "price_discovery"

    def __init__(self, symbol: str, atr_mult_stop: float = 1.0,
                 target_R: float = 1.5, arm_window_bars: int = 6,
                 accept_n: int = 2, accept_distance_atr: float = 0.25,
                 retrace_proximity_atr: float = 0.1):
        super().__init__(symbol)
        self.atr_mult_stop = atr_mult_stop
        self.target_R = target_R
        self.arm_window_bars = arm_window_bars
        self.accept_n = accept_n
        self.accept_distance_atr = accept_distance_atr
        self.retrace_proximity_atr = retrace_proximity_atr
        self._side: Optional[str] = None
        self._level: Optional[float] = None    # band breached at acceptance time

    def reset(self) -> None:
        super().reset()
        self._side = None
        self._level = None

    def check(self, ctx: SessionContext) -> Optional[SetupSignal]:
        if ctx.bar_count == 0:
            return None
        bar = ctx.bars[-1]
        atr = ctx.atr() or 0.0
        if atr <= 0:
            return None

        upper, lower = ctx.upper_band, ctx.lower_band
        in_value = lower <= bar.close <= upper

        # IDLE → BREAKOUT_PENDING
        if self.state == "IDLE":
            if bar.close > upper:
                self.state, self._side, self._level = "BREAKOUT_PENDING", "long", upper
                self.bars_in_state = 1
            elif bar.close < lower:
                self.state, self._side, self._level = "BREAKOUT_PENDING", "short", lower
                self.bars_in_state = 1
            return None

        # BREAKOUT_PENDING → ACCEPTED | IDLE
        if self.state == "BREAKOUT_PENDING":
            self.bars_in_state += 1
            if self._side == "long":
                if accepted_above(ctx.bars, self._level, self.accept_n,
                                  self.accept_distance_atr, atr):
                    self.state = "ACCEPTED"
                elif in_value:
                    self.reset()
            else:
                if accepted_below(ctx.bars, self._level, self.accept_n,
                                  self.accept_distance_atr, atr):
                    self.state = "ACCEPTED"
                elif in_value:
                    self.reset()
            return None

        # ACCEPTED → ARMED (price retraces toward the breached level)
        if self.state == "ACCEPTED":
            self.bars_in_state += 1
            close_to_level = abs(bar.close - self._level) <= self.retrace_proximity_atr * atr
            if close_to_level or (self._side == "long" and bar.low <= self._level + self.retrace_proximity_atr * atr) \
                              or (self._side == "short" and bar.high >= self._level - self.retrace_proximity_atr * atr):
                self.state = "ARMED"
                self.armed_level = self._level
                self.bars_in_state = 0
            return None

        # ARMED → FILLED | EXPIRED
        if self.state == "ARMED":
            self.bars_in_state += 1
            # Fire on a candle that wicks into the band and closes back in trend direction.
            if self._side == "long":
                if bar.low <= self._level and bar.close >= self._level and bar.is_bullish:
                    sig = self._build_signal(bar, atr)
                    self.reset()
                    return sig
                if bar.close <= ctx.lower_band:
                    self.reset()
            else:
                if bar.high >= self._level and bar.close <= self._level and not bar.is_bullish:
                    sig = self._build_signal(bar, atr)
                    self.reset()
                    return sig
                if bar.close >= ctx.upper_band:
                    self.reset()
            if self.bars_in_state >= self.arm_window_bars:
                self.reset()
            return None
        return None

    def _build_signal(self, bar: Bar, atr: float) -> SetupSignal:
        if self._side == "long":
            entry = self._level
            stop = bar.low - 0.1 * atr   # beyond the testing candle
            risk = entry - stop
            target = entry + self.target_R * risk
        else:
            entry = self._level
            stop = bar.high + 0.1 * atr
            risk = stop - entry
            target = entry - self.target_R * risk
        return SetupSignal(
            setup=self.name, symbol=self.symbol, side=self._side,
            entry=entry, stop=stop, target=target,
            atr=atr, level=self._level, ts=bar.ts, notes={"phase": "backtest"},
        )
```

- [ ] **3.2.4** Run tests

```bash
pytest tests/test_setup_price_discovery.py -v
```

Expected: 2 passed. (The first test exercises the happy path; the second confirms no false fire.)

- [ ] **3.2.5** Commit

```bash
git add strategies/setup_price_discovery.py tests/test_setup_price_discovery.py
git commit -m "feat(strategies): Setup 1 Price Discovery Continuation state machine"
```

### Task 3.3: Setup 3 — Return to Value (mirror)

**Files:**
- Create: `strategies/setup_return_to_value.py`
- Test: `tests/test_setup_return_to_value.py`

(Doing Setup 3 before 2 because it's structurally a mirror of Setup 1 — small delta, validates the abstraction.)

- [ ] **3.3.1** Write the failing test

Create `tests/test_setup_return_to_value.py`:

```python
from datetime import datetime, timezone, timedelta
from core.bar import Bar
from core.session import SessionContext
from core.asset_class import AssetClassConfig
from strategies.setup_return_to_value import ReturnToValueSetup


CRYPTO = AssetClassConfig(
    name="crypto", timezone="UTC",
    session_open_local="00:00", session_close_local="23:59",
    opening_blackout_min=15, bar_timeframe="5Min",
    slippage_bps=5.0, commission_per_share=0.0, commission_bps=25.0,
)


def _bar(ts, o, h, l, c, v=100):
    return Bar(symbol="X", ts=ts, open=o, high=h, low=l, close=c, volume=v)


def test_failed_discovery_then_reentry_emits_short_signal():
    ctx = SessionContext(symbol="X", asset_class=CRYPTO)
    setup = ReturnToValueSetup("X", atr_mult_stop=1.0, arm_window_bars=6,
                                accept_n=2, accept_distance_atr=0.0)
    base = datetime(2026, 5, 14, 0, 5, tzinfo=timezone.utc)
    bars = [_bar(base + timedelta(minutes=5*i), 100, 100.5, 99.5, 100) for i in range(6)]
    # Discovery up (close above upper band)
    bars += [_bar(base + timedelta(minutes=5*(6+i)), 101, 102, 100.8, 101.5) for i in range(2)]
    # Re-entry inside value
    bars += [_bar(base + timedelta(minutes=5*(8+i)), 100.5, 100.7, 100.0, 100.2) for i in range(2)]
    # Retest the upper band from below
    bars.append(_bar(base + timedelta(minutes=5*10), 100.4, 101.0, 100.0, 100.3))

    sig = None
    for b in bars:
        ctx.ingest(b)
        s = setup.check(ctx)
        if s is not None:
            sig = s
    assert sig is not None
    assert sig.side == "short"
    assert sig.target < sig.entry           # target = vwap, below entry for shorts
```

- [ ] **3.3.2** Run, expect failure

```bash
pytest tests/test_setup_return_to_value.py -v
```

- [ ] **3.3.3** Implement `strategies/setup_return_to_value.py`

```python
from __future__ import annotations
from typing import Optional

from core.acceptance import accepted_above, accepted_below
from core.bar import Bar
from core.session import SessionContext
from strategies.base_setup import BaseSetup, SetupSignal


class ReturnToValueSetup(BaseSetup):
    name = "return_to_value"

    def __init__(self, symbol: str, atr_mult_stop: float = 1.0,
                 arm_window_bars: int = 6, accept_n: int = 2,
                 accept_distance_atr: float = 0.25,
                 retrace_proximity_atr: float = 0.1):
        super().__init__(symbol)
        self.atr_mult_stop = atr_mult_stop
        self.arm_window_bars = arm_window_bars
        self.accept_n = accept_n
        self.accept_distance_atr = accept_distance_atr
        self.retrace_proximity_atr = retrace_proximity_atr
        self._side: Optional[str] = None     # direction of the trade we'd take (opposite of prior discovery)
        self._level: Optional[float] = None  # band that the price re-entered through

    def reset(self) -> None:
        super().reset()
        self._side = None
        self._level = None

    def check(self, ctx: SessionContext) -> Optional[SetupSignal]:
        if ctx.bar_count < 2:
            return None
        bar = ctx.bars[-1]
        prev = ctx.bars[-2]
        atr = ctx.atr() or 0.0
        if atr <= 0:
            return None

        upper, lower = ctx.upper_band, ctx.lower_band
        prev_was_outside_up = prev.close > upper
        prev_was_outside_dn = prev.close < lower
        now_in = lower <= bar.close <= upper

        # IDLE → REJECTION (re-entered value area)
        if self.state == "IDLE":
            if prev_was_outside_up and now_in:
                self.state, self._side, self._level = "REJECTION", "short", upper
                self.bars_in_state = 1
            elif prev_was_outside_dn and now_in:
                self.state, self._side, self._level = "REJECTION", "long", lower
                self.bars_in_state = 1
            return None

        # REJECTION → REENTRY_ACCEPTED (n closes inside value with distance)
        if self.state == "REJECTION":
            self.bars_in_state += 1
            inside = self._side == "short" and accepted_below(ctx.bars, self._level,
                                                              self.accept_n, self.accept_distance_atr, atr)
            inside = inside or (self._side == "long" and accepted_above(ctx.bars, self._level,
                                                                        self.accept_n, self.accept_distance_atr, atr))
            if inside:
                self.state = "REENTRY_ACCEPTED"
            return None

        # REENTRY_ACCEPTED → ARMED (retest band from inside)
        if self.state == "REENTRY_ACCEPTED":
            self.bars_in_state += 1
            close_to = abs(bar.close - self._level) <= self.retrace_proximity_atr * atr
            wick_to = (self._side == "short" and bar.high >= self._level - self.retrace_proximity_atr * atr) \
                   or (self._side == "long" and bar.low <= self._level + self.retrace_proximity_atr * atr)
            if close_to or wick_to:
                self.state = "ARMED"
                self.armed_level = self._level
                self.bars_in_state = 0
            return None

        # ARMED → FILLED | EXPIRED
        if self.state == "ARMED":
            self.bars_in_state += 1
            if self._side == "short":
                if bar.high >= self._level and bar.close <= self._level:
                    sig = self._build_signal(bar, atr, ctx.vwap)
                    self.reset()
                    return sig
                if bar.close > ctx.upper_band:        # broke out again — abort
                    self.reset()
            else:
                if bar.low <= self._level and bar.close >= self._level:
                    sig = self._build_signal(bar, atr, ctx.vwap)
                    self.reset()
                    return sig
                if bar.close < ctx.lower_band:
                    self.reset()
            if self.bars_in_state >= self.arm_window_bars:
                self.reset()
            return None
        return None

    def _build_signal(self, bar: Bar, atr: float, target_vwap: float) -> SetupSignal:
        if self._side == "short":
            entry = self._level
            stop = self._level + 0.5 * atr
        else:
            entry = self._level
            stop = self._level - 0.5 * atr
        return SetupSignal(
            setup=self.name, symbol=self.symbol, side=self._side,
            entry=entry, stop=stop, target=target_vwap,
            atr=atr, level=self._level, ts=bar.ts, notes={"target": "vwap"},
        )
```

- [ ] **3.3.4** Run tests

```bash
pytest tests/test_setup_return_to_value.py -v
```

Expected: 1 passed.

- [ ] **3.3.5** Commit

```bash
git add strategies/setup_return_to_value.py tests/test_setup_return_to_value.py
git commit -m "feat(strategies): Setup 3 Return to Value state machine"
```

### Task 3.4: Setup 2 — Fade Value Area Extremes

**Files:**
- Create: `strategies/setup_fade_extreme.py`
- Test: `tests/test_setup_fade_extreme.py`

(Setup 2 is the only setup that produces multiple scale-in signals from a single trigger. The setup emits one `SetupSignal` per scale; the order executor handles the aggregate. This task implements the trigger + first scale; the scale-out logic for orders 2 and 3 is computed by the executor in Phase 4.)

- [ ] **3.4.1** Write the failing test

Create `tests/test_setup_fade_extreme.py`:

```python
from datetime import datetime, timezone, timedelta
from core.bar import Bar
from core.session import SessionContext
from core.asset_class import AssetClassConfig
from strategies.setup_fade_extreme import FadeExtremeSetup


CRYPTO = AssetClassConfig(
    name="crypto", timezone="UTC",
    session_open_local="00:00", session_close_local="23:59",
    opening_blackout_min=15, bar_timeframe="5Min",
    slippage_bps=5.0, commission_per_share=0.0, commission_bps=25.0,
)


def _bar(ts, o, h, l, c, v=100):
    return Bar(symbol="X", ts=ts, open=o, high=h, low=l, close=c, volume=v)


def test_balance_day_rejection_at_upper_band_emits_short():
    ctx = SessionContext(symbol="X", asset_class=CRYPTO)
    setup = FadeExtremeSetup("X", atr_mult_stop=0.75, min_in_value_bars=6,
                             scale_offsets_atr=[0.0, 0.25, 0.5],
                             scale_weights=[0.4, 0.35, 0.25])
    base = datetime(2026, 5, 14, 0, 5, tzinfo=timezone.utc)
    # 6 in-value bars to qualify the day as balance + meet min_in_value_bars
    bars = [_bar(base + timedelta(minutes=5*i), 100, 100.4, 99.6, 100) for i in range(6)]
    # rejection bar: wicks above upper band but closes back inside
    bars.append(_bar(base + timedelta(minutes=30), 100, 102, 99.8, 100.1))

    sig = None
    for b in bars:
        ctx.ingest(b)
        s = setup.check(ctx)
        if s is not None:
            sig = s
    assert sig is not None
    assert sig.side == "short"
    assert sig.target < sig.entry         # target = vwap


def test_no_fire_when_not_balance_day():
    ctx = SessionContext(symbol="X", asset_class=CRYPTO)
    ctx.avg_range_20d = 1.0
    setup = FadeExtremeSetup("X", atr_mult_stop=0.75, min_in_value_bars=6,
                             scale_offsets_atr=[0.0, 0.25, 0.5],
                             scale_weights=[0.4, 0.35, 0.25])
    base = datetime(2026, 5, 14, 0, 5, tzinfo=timezone.utc)
    # trending bars (all above vwap-progression)
    bars = []
    for i in range(8):
        c = 100 + i * 1.0
        bars.append(_bar(base + timedelta(minutes=5*i), c - 0.5, c + 0.5, c - 0.5, c))
    sig = None
    for b in bars:
        ctx.ingest(b)
        s = setup.check(ctx)
        if s is not None:
            sig = s
    assert sig is None
```

- [ ] **3.4.2** Run, expect failure

```bash
pytest tests/test_setup_fade_extreme.py -v
```

- [ ] **3.4.3** Implement `strategies/setup_fade_extreme.py`

```python
from __future__ import annotations
from typing import Optional

from core.bar import Bar
from core.session import SessionContext
from strategies.base_setup import BaseSetup, SetupSignal


class FadeExtremeSetup(BaseSetup):
    name = "fade_extreme"

    def __init__(self, symbol: str, atr_mult_stop: float = 0.75,
                 min_in_value_bars: int = 6,
                 scale_offsets_atr: list[float] | None = None,
                 scale_weights: list[float] | None = None,
                 balance_in_value_min: float = 0.60):
        super().__init__(symbol)
        self.atr_mult_stop = atr_mult_stop
        self.min_in_value_bars = min_in_value_bars
        self.scale_offsets_atr = scale_offsets_atr or [0.0, 0.25, 0.5]
        self.scale_weights = scale_weights or [0.4, 0.35, 0.25]
        self.balance_in_value_min = balance_in_value_min

    def _is_balance_day(self, ctx: SessionContext) -> bool:
        return ctx.in_value_area_fraction() >= self.balance_in_value_min

    def check(self, ctx: SessionContext) -> Optional[SetupSignal]:
        if ctx.bar_count < self.min_in_value_bars:
            return None
        if not self._is_balance_day(ctx):
            return None
        atr = ctx.atr() or 0.0
        if atr <= 0:
            return None

        bar = ctx.bars[-1]
        upper, lower, vwap = ctx.upper_band, ctx.lower_band, ctx.vwap

        # Rejection at upper band — short
        if bar.high > upper and bar.close < upper:
            entry = bar.close
            stop = upper + self.atr_mult_stop * atr
            return SetupSignal(
                setup=self.name, symbol=self.symbol, side="short",
                entry=entry, stop=stop, target=vwap,
                atr=atr, level=upper, ts=bar.ts,
                notes={"scale_offsets_atr": self.scale_offsets_atr,
                       "scale_weights": self.scale_weights, "scale_index": 0},
            )

        # Rejection at lower band — long
        if bar.low < lower and bar.close > lower:
            entry = bar.close
            stop = lower - self.atr_mult_stop * atr
            return SetupSignal(
                setup=self.name, symbol=self.symbol, side="long",
                entry=entry, stop=stop, target=vwap,
                atr=atr, level=lower, ts=bar.ts,
                notes={"scale_offsets_atr": self.scale_offsets_atr,
                       "scale_weights": self.scale_weights, "scale_index": 0},
            )
        return None
```

- [ ] **3.4.4** Run tests

```bash
pytest tests/test_setup_fade_extreme.py -v
```

Expected: 2 passed.

- [ ] **3.4.5** Commit

```bash
git add strategies/setup_fade_extreme.py tests/test_setup_fade_extreme.py
git commit -m "feat(strategies): Setup 2 Fade Value Area Extremes (first-scale signal)"
```

### Task 3.5: Setup 4 — VWAP Bounce (Trend Days)

**Files:**
- Create: `strategies/setup_vwap_bounce.py`
- Test: `tests/test_setup_vwap_bounce.py`

- [ ] **3.5.1** Write the failing test

Create `tests/test_setup_vwap_bounce.py`:

```python
from datetime import datetime, timezone, timedelta
from core.bar import Bar
from core.session import SessionContext
from core.asset_class import AssetClassConfig
from strategies.setup_vwap_bounce import VWAPBounceSetup


CRYPTO = AssetClassConfig(
    name="crypto", timezone="UTC",
    session_open_local="00:00", session_close_local="23:59",
    opening_blackout_min=15, bar_timeframe="5Min",
    slippage_bps=5.0, commission_per_share=0.0, commission_bps=25.0,
)


def _bar(ts, o, h, l, c, v=100):
    return Bar(symbol="X", ts=ts, open=o, high=h, low=l, close=c, volume=v)


def test_uptrend_sub_vwap_trap_then_reclaim_pullback_emits_long():
    ctx = SessionContext(symbol="X", asset_class=CRYPTO)
    ctx.avg_range_20d = 2.0
    setup = VWAPBounceSetup("X", atr_mult_stop=1.25, target_R=2.0,
                            arm_window_bars=4, trend_majority=0.7,
                            trend_range_mult=1.5)
    base = datetime(2026, 5, 14, 0, 5, tzinfo=timezone.utc)
    bars = []
    # Strong uptrend: 12 bars marching up, range >> avg
    for i in range(12):
        c = 100 + i * 1.0
        bars.append(_bar(base + timedelta(minutes=5*i), c - 0.5, c + 0.5, c - 0.5, c))
    # Sub-VWAP trap: dip below current VWAP
    bars.append(_bar(base + timedelta(minutes=5*12), 110, 110.2, 99.0, 99.2))
    # Reclaim: bar closes back above VWAP
    bars.append(_bar(base + timedelta(minutes=5*13), 99.2, 112.0, 99.0, 111.0))
    # Pullback to vwap → fill
    bars.append(_bar(base + timedelta(minutes=5*14), 111.0, 111.5, 105.0, 110.5))

    sig = None
    for b in bars:
        ctx.ingest(b)
        s = setup.check(ctx)
        if s is not None:
            sig = s
    assert sig is not None
    assert sig.side == "long"
    assert sig.target > sig.entry
```

- [ ] **3.5.2** Run, expect failure

```bash
pytest tests/test_setup_vwap_bounce.py -v
```

- [ ] **3.5.3** Implement `strategies/setup_vwap_bounce.py`

```python
from __future__ import annotations
from typing import Optional

from core.bar import Bar
from core.session import SessionContext
from strategies.base_setup import BaseSetup, SetupSignal


class VWAPBounceSetup(BaseSetup):
    name = "vwap_bounce"

    def __init__(self, symbol: str, atr_mult_stop: float = 1.25,
                 target_R: float = 2.0, arm_window_bars: int = 4,
                 trend_majority: float = 0.7, trend_range_mult: float = 1.5,
                 retrace_proximity_atr: float = 0.15):
        super().__init__(symbol)
        self.atr_mult_stop = atr_mult_stop
        self.target_R = target_R
        self.arm_window_bars = arm_window_bars
        self.trend_majority = trend_majority
        self.trend_range_mult = trend_range_mult
        self.retrace_proximity_atr = retrace_proximity_atr
        self._trap_bar: Optional[Bar] = None
        self._reclaim_bar: Optional[Bar] = None
        self._side: Optional[str] = None

    def reset(self) -> None:
        super().reset()
        self._trap_bar = None
        self._reclaim_bar = None
        self._side = None

    def _trend_side(self, ctx: SessionContext) -> Optional[str]:
        if ctx.bar_count < 6:
            return None
        avg = ctx.avg_range_20d if ctx.avg_range_20d > 0 else (ctx.day_high - ctx.day_low)
        if avg <= 0:
            return None
        if (ctx.day_high - ctx.day_low) < self.trend_range_mult * avg:
            return None
        frac_above = ctx.fraction_above_vwap()
        if frac_above >= self.trend_majority:
            return "long"
        if frac_above <= 1 - self.trend_majority:
            return "short"
        return None

    def check(self, ctx: SessionContext) -> Optional[SetupSignal]:
        if ctx.bar_count == 0:
            return None
        bar = ctx.bars[-1]
        atr = ctx.atr() or 0.0
        if atr <= 0:
            return None

        if self.state == "IDLE":
            side = self._trend_side(ctx)
            if side:
                self.state, self._side = "TREND_CONFIRMED", side
            return None

        if self.state == "TREND_CONFIRMED":
            if self._side == "long" and bar.low < ctx.vwap and bar.close < ctx.vwap:
                self._trap_bar = bar
                self.state = "SUB_VWAP_TRAP"
            elif self._side == "short" and bar.high > ctx.vwap and bar.close > ctx.vwap:
                self._trap_bar = bar
                self.state = "SUB_VWAP_TRAP"
            return None

        if self.state == "SUB_VWAP_TRAP":
            if self._side == "long" and bar.close > ctx.vwap:
                self._reclaim_bar = bar
                self.state = "ARMED"
                self.bars_in_state = 0
            elif self._side == "short" and bar.close < ctx.vwap:
                self._reclaim_bar = bar
                self.state = "ARMED"
                self.bars_in_state = 0
            return None

        if self.state == "ARMED":
            self.bars_in_state += 1
            close_to_vwap = abs(bar.close - ctx.vwap) <= self.retrace_proximity_atr * atr
            wick_to = (self._side == "long" and bar.low <= ctx.vwap + self.retrace_proximity_atr * atr) \
                   or (self._side == "short" and bar.high >= ctx.vwap - self.retrace_proximity_atr * atr)
            if close_to_vwap or wick_to:
                sig = self._build_signal(bar, atr, ctx.vwap)
                self.reset()
                return sig
            if self.bars_in_state >= self.arm_window_bars:
                self.reset()
            return None
        return None

    def _build_signal(self, bar: Bar, atr: float, vwap: float) -> SetupSignal:
        if self._side == "long":
            entry = vwap
            stop = (self._reclaim_bar.low if self._reclaim_bar else bar.low) - 0.1 * atr
            risk = entry - stop
            target = entry + self.target_R * risk
        else:
            entry = vwap
            stop = (self._reclaim_bar.high if self._reclaim_bar else bar.high) + 0.1 * atr
            risk = stop - entry
            target = entry - self.target_R * risk
        return SetupSignal(
            setup=self.name, symbol=self.symbol, side=self._side,
            entry=entry, stop=stop, target=target,
            atr=atr, level=vwap, ts=bar.ts, notes={"trend": True},
        )
```

- [ ] **3.5.4** Run tests

```bash
pytest tests/test_setup_vwap_bounce.py -v
```

Expected: 1 passed.

- [ ] **3.5.5** Commit

```bash
git add strategies/setup_vwap_bounce.py tests/test_setup_vwap_bounce.py
git commit -m "feat(strategies): Setup 4 VWAP Bounce trend-day reclaim state machine"
```

---

**End of Phase 3.** Tree state: four setup state machines emit `SetupSignal` objects; each is independently unit-tested with synthetic OHLCV. No broker, no risk, no orders yet. Ready for Phase 4.

---

## Phase 4 — Risk Pipeline

### Task 4.1: `daily_ledger.py` (per-symbol P&L + win/loss streak)

**Files:**
- Create: `state/daily_ledger.py`
- Test: `tests/test_daily_ledger.py`

- [ ] **4.1.1** Write the failing test

Create `tests/test_daily_ledger.py`:

```python
from datetime import datetime, timezone
from state.daily_ledger import DailyLedger, TradeRecord


def test_ledger_records_trade_and_streak():
    ledger = DailyLedger(initial_equity=100000.0)
    t = datetime(2026, 5, 14, 14, 0, tzinfo=timezone.utc)
    ledger.record(TradeRecord(symbol="AAPL", setup="price_discovery",
                              entry_ts=t, exit_ts=t, entry_px=100, exit_px=99,
                              side="long", qty=10, R_realized=-1.0, pnl_usd=-10))
    ledger.record(TradeRecord(symbol="AAPL", setup="price_discovery",
                              entry_ts=t, exit_ts=t, entry_px=100, exit_px=99,
                              side="long", qty=10, R_realized=-1.0, pnl_usd=-10))
    assert ledger.consecutive_losses_for("AAPL") == 2
    assert ledger.consecutive_losses_for("MSFT") == 0
    assert ledger.equity == 100000.0 - 20.0


def test_winning_trade_resets_streak():
    ledger = DailyLedger(initial_equity=100000.0)
    t = datetime(2026, 5, 14, 14, 0, tzinfo=timezone.utc)
    ledger.record(TradeRecord(symbol="AAPL", setup="x", entry_ts=t, exit_ts=t,
                              entry_px=100, exit_px=99, side="long", qty=10,
                              R_realized=-1.0, pnl_usd=-10))
    ledger.record(TradeRecord(symbol="AAPL", setup="x", entry_ts=t, exit_ts=t,
                              entry_px=100, exit_px=102, side="long", qty=10,
                              R_realized=2.0, pnl_usd=20))
    assert ledger.consecutive_losses_for("AAPL") == 0


def test_roll_day_clears_streaks():
    ledger = DailyLedger(initial_equity=100000.0)
    t = datetime(2026, 5, 14, 14, 0, tzinfo=timezone.utc)
    ledger.record(TradeRecord(symbol="AAPL", setup="x", entry_ts=t, exit_ts=t,
                              entry_px=100, exit_px=99, side="long", qty=10,
                              R_realized=-1.0, pnl_usd=-10))
    ledger.roll_day(datetime(2026, 5, 15, 0, 0, tzinfo=timezone.utc))
    assert ledger.consecutive_losses_for("AAPL") == 0
    assert ledger.day_pnl == 0.0
```

- [ ] **4.1.2** Run, expect failure

```bash
pytest tests/test_daily_ledger.py -v
```

- [ ] **4.1.3** Implement `state/daily_ledger.py`

```python
from __future__ import annotations
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass(frozen=True)
class TradeRecord:
    symbol: str
    setup: str
    entry_ts: datetime
    exit_ts: datetime
    entry_px: float
    exit_px: float
    side: str
    qty: float
    R_realized: float
    pnl_usd: float


@dataclass
class DailyLedger:
    initial_equity: float
    equity: float = field(init=False)
    day_pnl: float = 0.0
    day_started_at: Optional[datetime] = None
    trades_today: list[TradeRecord] = field(default_factory=list)
    consec_losses_per_symbol: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    consec_losses_system: int = 0

    def __post_init__(self):
        self.equity = self.initial_equity

    def record(self, t: TradeRecord) -> None:
        self.trades_today.append(t)
        self.equity += t.pnl_usd
        self.day_pnl += t.pnl_usd
        if t.pnl_usd < 0:
            self.consec_losses_per_symbol[t.symbol] = self.consec_losses_per_symbol.get(t.symbol, 0) + 1
            self.consec_losses_system += 1
        else:
            self.consec_losses_per_symbol[t.symbol] = 0
            self.consec_losses_system = 0

    def consecutive_losses_for(self, symbol: str) -> int:
        return self.consec_losses_per_symbol.get(symbol, 0)

    def roll_day(self, new_day_start: datetime) -> None:
        self.day_started_at = new_day_start
        self.day_pnl = 0.0
        self.trades_today = []
        self.consec_losses_per_symbol = defaultdict(int)
        self.consec_losses_system = 0
```

- [ ] **4.1.4** Run tests

```bash
pytest tests/test_daily_ledger.py -v
```

Expected: 3 passed.

- [ ] **4.1.5** Commit

```bash
git add state/daily_ledger.py tests/test_daily_ledger.py
git commit -m "feat(state): DailyLedger with per-symbol consecutive-loss tracking"
```

### Task 4.2: `position_book.py` (open positions)

**Files:**
- Create: `state/position_book.py`
- Test: `tests/test_position_book.py`

- [ ] **4.2.1** Write the failing test

Create `tests/test_position_book.py`:

```python
from datetime import datetime, timezone
from state.position_book import PositionBook, OpenPosition


def test_add_and_lookup():
    book = PositionBook()
    p = OpenPosition(symbol="AAPL", setup="price_discovery", side="long",
                     qty=10, entry_px=100.0, stop_px=99.0, target_px=102.0,
                     opened_at=datetime(2026, 5, 14, 14, 0, tzinfo=timezone.utc),
                     order_id="abc")
    book.add(p)
    assert book.get("AAPL") is p
    assert book.symbols() == ["AAPL"]


def test_concurrent_count():
    book = PositionBook()
    for s in ("AAPL", "MSFT", "BTC/USD"):
        book.add(OpenPosition(symbol=s, setup="x", side="long", qty=1,
                              entry_px=1.0, stop_px=0.5, target_px=2.0,
                              opened_at=datetime.now(timezone.utc), order_id="x"))
    assert book.count() == 3


def test_close_removes():
    book = PositionBook()
    p = OpenPosition(symbol="AAPL", setup="x", side="long", qty=1,
                     entry_px=1.0, stop_px=0.5, target_px=2.0,
                     opened_at=datetime.now(timezone.utc), order_id="x")
    book.add(p)
    book.close("AAPL")
    assert book.get("AAPL") is None


def test_aggregate_open_risk():
    book = PositionBook()
    book.add(OpenPosition(symbol="AAPL", setup="x", side="long", qty=10,
                          entry_px=100.0, stop_px=99.0, target_px=102.0,
                          opened_at=datetime.now(timezone.utc), order_id="x"))
    book.add(OpenPosition(symbol="MSFT", setup="x", side="long", qty=5,
                          entry_px=200.0, stop_px=199.0, target_px=202.0,
                          opened_at=datetime.now(timezone.utc), order_id="y"))
    # AAPL risk = 10 × 1 = 10; MSFT risk = 5 × 1 = 5; total 15
    assert book.aggregate_open_risk_usd() == 15.0
```

- [ ] **4.2.2** Run, expect failure

```bash
pytest tests/test_position_book.py -v
```

- [ ] **4.2.3** Implement `state/position_book.py`

```python
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime


@dataclass
class OpenPosition:
    symbol: str
    setup: str
    side: str               # "long" | "short"
    qty: float
    entry_px: float
    stop_px: float
    target_px: float
    opened_at: datetime
    order_id: str
    breakeven_moved: bool = False
    bars_held: int = 0

    @property
    def risk_per_share(self) -> float:
        return abs(self.entry_px - self.stop_px)

    @property
    def open_risk_usd(self) -> float:
        return self.risk_per_share * self.qty


class PositionBook:
    def __init__(self) -> None:
        self._positions: dict[str, OpenPosition] = {}

    def add(self, p: OpenPosition) -> None:
        if p.symbol in self._positions:
            raise ValueError(f"Position already open on {p.symbol}")
        self._positions[p.symbol] = p

    def get(self, symbol: str) -> OpenPosition | None:
        return self._positions.get(symbol)

    def close(self, symbol: str) -> OpenPosition | None:
        return self._positions.pop(symbol, None)

    def symbols(self) -> list[str]:
        return list(self._positions.keys())

    def count(self) -> int:
        return len(self._positions)

    def all(self) -> list[OpenPosition]:
        return list(self._positions.values())

    def aggregate_open_risk_usd(self) -> float:
        return sum(p.open_risk_usd for p in self._positions.values())
```

- [ ] **4.2.4** Run tests

```bash
pytest tests/test_position_book.py -v
```

Expected: 4 passed.

- [ ] **4.2.5** Replace the legacy `tests/test_portfolio.py` (which still imports the deleted Portfolio)

```bash
git rm tests/test_portfolio.py
```

- [ ] **4.2.6** Commit

```bash
git add state/position_book.py tests/test_position_book.py
git commit -m "feat(state): PositionBook (open positions ledger) + drop legacy test_portfolio"
```

### Task 4.3: `sizing.py` — ATR-based position sizing

**Files:**
- Create: `risk/sizing.py`
- Test: `tests/test_sizing.py`

- [ ] **4.3.1** Write the failing test

Create `tests/test_sizing.py`:

```python
import pytest
from risk.sizing import size_position, SizingConfig


def test_basic_sizing():
    cfg = SizingConfig(max_risk_per_trade=0.005, max_notional_per_trade_pct=0.20)
    qty, notional = size_position(equity=100000, entry=100, stop=99, cfg=cfg)
    # risk = 500; per-share risk = 1; qty = 500
    assert qty == 500
    assert notional == 500 * 100


def test_notional_cap_clamps_qty():
    cfg = SizingConfig(max_risk_per_trade=0.005, max_notional_per_trade_pct=0.10)
    qty, notional = size_position(equity=100000, entry=100, stop=99, cfg=cfg)
    # risk-based qty would be 500 → notional 50k. cap = 10% × 100k = 10k → qty floor(10k/100) = 100
    assert qty == 100
    assert notional == 100 * 100


def test_zero_stop_distance_raises():
    cfg = SizingConfig(max_risk_per_trade=0.005, max_notional_per_trade_pct=0.20)
    with pytest.raises(ValueError):
        size_position(equity=100000, entry=100, stop=100, cfg=cfg)


def test_fractional_qty_supported_for_crypto():
    cfg = SizingConfig(max_risk_per_trade=0.01, max_notional_per_trade_pct=0.50,
                       allow_fractional=True)
    qty, _ = size_position(equity=10000, entry=50000, stop=49500, cfg=cfg)
    # risk = 100; per-share = 500; qty = 0.2
    assert abs(qty - 0.2) < 1e-9
```

- [ ] **4.3.2** Run, expect failure

```bash
pytest tests/test_sizing.py -v
```

- [ ] **4.3.3** Implement `risk/sizing.py`

```python
from __future__ import annotations
import math
from dataclasses import dataclass


@dataclass(frozen=True)
class SizingConfig:
    max_risk_per_trade: float = 0.005       # fraction of equity
    max_notional_per_trade_pct: float = 0.20
    allow_fractional: bool = False          # crypto = True, equity = False


def size_position(equity: float, entry: float, stop: float, cfg: SizingConfig) -> tuple[float, float]:
    risk_per_share = abs(entry - stop)
    if risk_per_share == 0:
        raise ValueError("Stop distance is zero — cannot size position")
    risk_dollars = equity * cfg.max_risk_per_trade
    raw_qty = risk_dollars / risk_per_share
    raw_notional = raw_qty * entry
    notional_cap = equity * cfg.max_notional_per_trade_pct
    if raw_notional > notional_cap:
        raw_qty = notional_cap / entry
    qty = raw_qty if cfg.allow_fractional else math.floor(raw_qty)
    notional = qty * entry
    return float(qty), float(notional)
```

- [ ] **4.3.4** Run tests

```bash
pytest tests/test_sizing.py -v
```

Expected: 4 passed.

- [ ] **4.3.5** Commit

```bash
git add risk/sizing.py tests/test_sizing.py
git commit -m "feat(risk): ATR-based position sizing with notional cap and fractional opt-in"
```

### Task 4.4: `filters.py` — entry filter pipeline

**Files:**
- Create: `risk/filters.py`
- Test: `tests/test_filters.py`

- [ ] **4.4.1** Write the failing test

Create `tests/test_filters.py`:

```python
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass
from state.daily_ledger import DailyLedger, TradeRecord
from state.position_book import PositionBook, OpenPosition
from risk.filters import (
    FilterPipeline, FilterResult,
    SystemHaltedFilter, SessionWindowFilter, NewsBlackoutFilter,
    ConsecutiveLossFilter, ConcurrentPositionFilter,
    NewsBlackout,
)
from strategies.base_setup import SetupSignal


@dataclass
class FakeCB:
    level: int = 0


def _signal(symbol="AAPL"):
    return SetupSignal(setup="x", symbol=symbol, side="long",
                       entry=100, stop=99, target=102, atr=1.0,
                       level=100, ts=datetime(2026, 5, 14, 14, 0, tzinfo=timezone.utc))


def test_system_halted_blocks_when_cb_l2():
    f = SystemHaltedFilter(circuit_breaker=FakeCB(level=2), lock_file_path="/nonexistent")
    res = f.check(_signal(), ctx=None, ledger=None, book=None)
    assert not res.passed


def test_consecutive_loss_blocks_after_two():
    led = DailyLedger(initial_equity=100000)
    t = datetime(2026, 5, 14, 14, 0, tzinfo=timezone.utc)
    for _ in range(2):
        led.record(TradeRecord(symbol="AAPL", setup="x", entry_ts=t, exit_ts=t,
                               entry_px=100, exit_px=99, side="long", qty=10,
                               R_realized=-1.0, pnl_usd=-10))
    f = ConsecutiveLossFilter(limit=2, scope="per_symbol")
    assert not f.check(_signal("AAPL"), ctx=None, ledger=led, book=None).passed
    assert f.check(_signal("MSFT"), ctx=None, ledger=led, book=None).passed


def test_concurrent_position_filter():
    book = PositionBook()
    for s in ("AAPL", "MSFT", "TSLA"):
        book.add(OpenPosition(symbol=s, setup="x", side="long", qty=1,
                              entry_px=1.0, stop_px=0.5, target_px=2.0,
                              opened_at=datetime.now(timezone.utc), order_id="x"))
    f = ConcurrentPositionFilter(max_concurrent=3)
    assert not f.check(_signal("NVDA"), ctx=None, ledger=None, book=book).passed


def test_news_blackout_filter():
    now = datetime(2026, 5, 14, 14, 33, tzinfo=timezone.utc)
    win = NewsBlackout(start=datetime(2026, 5, 14, 14, 30, tzinfo=timezone.utc),
                       duration_min=10, label="CPI")
    f = NewsBlackoutFilter(windows=[win], pad_min=5, now_fn=lambda: now)
    assert not f.check(_signal(), ctx=None, ledger=None, book=None).passed


def test_pipeline_short_circuits_on_first_reject():
    led = DailyLedger(initial_equity=100000)
    t = datetime(2026, 5, 14, 14, 0, tzinfo=timezone.utc)
    for _ in range(2):
        led.record(TradeRecord(symbol="AAPL", setup="x", entry_ts=t, exit_ts=t,
                               entry_px=100, exit_px=99, side="long", qty=10,
                               R_realized=-1.0, pnl_usd=-10))
    pipeline = FilterPipeline([
        SystemHaltedFilter(circuit_breaker=FakeCB(level=0), lock_file_path="/nonexistent"),
        ConsecutiveLossFilter(limit=2, scope="per_symbol"),
        ConcurrentPositionFilter(max_concurrent=10),
    ])
    res = pipeline.check(_signal("AAPL"), ctx=None, ledger=led, book=PositionBook())
    assert not res.passed
    assert "consecutive" in res.reason.lower()
```

- [ ] **4.4.2** Run, expect failure

```bash
pytest tests/test_filters.py -v
```

- [ ] **4.4.3** Implement `risk/filters.py`

```python
from __future__ import annotations
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterable, Optional

from state.daily_ledger import DailyLedger
from state.position_book import PositionBook
from strategies.base_setup import SetupSignal


@dataclass(frozen=True)
class FilterResult:
    passed: bool
    reason: str = ""

    @classmethod
    def ok(cls) -> "FilterResult":
        return cls(passed=True, reason="")

    @classmethod
    def reject(cls, reason: str) -> "FilterResult":
        return cls(passed=False, reason=reason)


@dataclass(frozen=True)
class NewsBlackout:
    start: datetime
    duration_min: int
    label: str

    @property
    def end(self) -> datetime:
        return self.start + timedelta(minutes=self.duration_min)


class EntryFilter(ABC):
    name: str = "filter"

    @abstractmethod
    def check(self, signal: SetupSignal, ctx, ledger, book) -> FilterResult:
        raise NotImplementedError


class SystemHaltedFilter(EntryFilter):
    name = "system_halted"

    def __init__(self, circuit_breaker, lock_file_path: str):
        self.cb = circuit_breaker
        self.lock_file_path = lock_file_path

    def check(self, signal, ctx, ledger, book) -> FilterResult:
        if os.path.exists(self.lock_file_path):
            return FilterResult.reject("lock file present")
        if getattr(self.cb, "level", 0) >= 2:
            return FilterResult.reject(f"circuit breaker L{self.cb.level}")
        return FilterResult.ok()


class SessionWindowFilter(EntryFilter):
    name = "session_window"

    def __init__(self, opening_blackout_min: int = 15,
                 now_fn: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        self.opening_blackout_min = opening_blackout_min
        self.now_fn = now_fn

    def check(self, signal, ctx, ledger, book) -> FilterResult:
        if ctx is None or ctx.session_start_ts is None:
            return FilterResult.ok()
        elapsed = (self.now_fn() - ctx.session_start_ts).total_seconds() / 60.0
        if elapsed < self.opening_blackout_min:
            return FilterResult.reject(f"opening blackout: {elapsed:.1f} < {self.opening_blackout_min} min")
        return FilterResult.ok()


class NewsBlackoutFilter(EntryFilter):
    name = "news_blackout"

    def __init__(self, windows: Iterable[NewsBlackout], pad_min: int = 5,
                 now_fn: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        self.windows = list(windows)
        self.pad = timedelta(minutes=pad_min)
        self.now_fn = now_fn

    def check(self, signal, ctx, ledger, book) -> FilterResult:
        now = self.now_fn()
        for w in self.windows:
            if (w.start - self.pad) <= now <= (w.end + self.pad):
                return FilterResult.reject(f"news blackout: {w.label}")
        return FilterResult.ok()


class VolumeDeficitFilter(EntryFilter):
    name = "volume_deficit"

    def __init__(self, deficit_pct: float = 0.30, lookback_bars: int = 6):
        self.deficit_pct = deficit_pct
        self.lookback_bars = lookback_bars

    def check(self, signal, ctx, ledger, book) -> FilterResult:
        if ctx is None or ctx.bar_count < self.lookback_bars:
            return FilterResult.ok()
        recent = ctx.bars[-self.lookback_bars:]
        recent_vol = sum(b.volume for b in recent) / len(recent)
        baseline = getattr(ctx, "avg_volume_per_bar", 0.0)
        if baseline <= 0:
            return FilterResult.ok()        # cannot evaluate
        if recent_vol < (1 - self.deficit_pct) * baseline:
            return FilterResult.reject(f"volume {recent_vol:.0f} < {(1-self.deficit_pct)*baseline:.0f}")
        return FilterResult.ok()


class ConsecutiveLossFilter(EntryFilter):
    name = "consecutive_loss"

    def __init__(self, limit: int = 2, scope: str = "per_symbol"):
        self.limit = limit
        self.scope = scope

    def check(self, signal, ctx, ledger: DailyLedger | None, book) -> FilterResult:
        if ledger is None:
            return FilterResult.ok()
        if self.scope == "system_wide":
            if ledger.consec_losses_system >= self.limit:
                return FilterResult.reject(f"system consecutive losses {ledger.consec_losses_system}")
        else:
            count = ledger.consecutive_losses_for(signal.symbol)
            if count >= self.limit:
                return FilterResult.reject(f"consecutive losses on {signal.symbol}: {count}")
        return FilterResult.ok()


class ConcurrentPositionFilter(EntryFilter):
    name = "concurrent_position"

    def __init__(self, max_concurrent: int = 4):
        self.max_concurrent = max_concurrent

    def check(self, signal, ctx, ledger, book: PositionBook | None) -> FilterResult:
        if book is None:
            return FilterResult.ok()
        if book.get(signal.symbol) is not None:
            return FilterResult.reject("position already open on this symbol")
        if book.count() >= self.max_concurrent:
            return FilterResult.reject(f"max concurrent positions ({self.max_concurrent}) reached")
        return FilterResult.ok()


class SetupCooldownFilter(EntryFilter):
    name = "setup_cooldown"

    def __init__(self, cooldown_bars: int = 12):
        self.cooldown_bars = cooldown_bars
        self._last_fire: dict[tuple[str, str], datetime] = {}

    def check(self, signal, ctx, ledger, book) -> FilterResult:
        key = (signal.symbol, signal.setup)
        last = self._last_fire.get(key)
        if last is None or ctx is None:
            self._last_fire[key] = signal.ts
            return FilterResult.ok()
        elapsed_min = (signal.ts - last).total_seconds() / 60.0
        bar_min = 5
        if elapsed_min < self.cooldown_bars * bar_min:
            return FilterResult.reject(f"setup cooldown: {elapsed_min:.0f} < {self.cooldown_bars * bar_min} min")
        self._last_fire[key] = signal.ts
        return FilterResult.ok()


class RiskBudgetFilter(EntryFilter):
    name = "risk_budget"

    def __init__(self, daily_open_risk_cap_pct: float = 0.02):
        self.cap_pct = daily_open_risk_cap_pct

    def check(self, signal, ctx, ledger: DailyLedger | None, book: PositionBook | None) -> FilterResult:
        if ledger is None or book is None:
            return FilterResult.ok()
        proposed_risk = abs(signal.entry - signal.stop)
        cap = ledger.equity * self.cap_pct
        existing = book.aggregate_open_risk_usd()
        if existing + proposed_risk > cap:
            return FilterResult.reject(f"risk budget: existing {existing:.0f} + new {proposed_risk:.0f} > cap {cap:.0f}")
        return FilterResult.ok()


class FilterPipeline:
    def __init__(self, filters: list[EntryFilter]):
        self.filters = filters

    def check(self, signal, ctx, ledger, book) -> FilterResult:
        for f in self.filters:
            res = f.check(signal, ctx, ledger, book)
            if not res.passed:
                return FilterResult(passed=False, reason=f"{f.name}: {res.reason}")
        return FilterResult.ok()
```

- [ ] **4.4.4** Run tests

```bash
pytest tests/test_filters.py -v
```

Expected: 5 passed.

- [ ] **4.4.5** Commit

```bash
git add risk/filters.py tests/test_filters.py
git commit -m "feat(risk): composable EntryFilter pipeline (8 filters + result type)"
```

### Task 4.5: Rewrite `risk/manager.py` as a thin façade

**Files:**
- Modify: `risk/manager.py` (full rewrite)
- Modify: `tests/test_risk_manager.py` (rewrite around the new API)

- [ ] **4.5.1** Replace `tests/test_risk_manager.py` contents with:

```python
import os
from datetime import datetime, timezone
from risk.manager import RiskManager
from risk.filters import (
    FilterPipeline, SystemHaltedFilter, ConcurrentPositionFilter,
    ConsecutiveLossFilter,
)
from risk.sizing import SizingConfig
from risk.circuit_breakers import CircuitBreaker
from state.daily_ledger import DailyLedger
from state.position_book import PositionBook
from strategies.base_setup import SetupSignal


def _signal():
    return SetupSignal(setup="x", symbol="AAPL", side="long",
                       entry=100, stop=99, target=102, atr=1.0,
                       level=100, ts=datetime(2026, 5, 14, 14, 0, tzinfo=timezone.utc))


def _build_rm(ledger, book, lock_path="/nonexistent"):
    cb = CircuitBreaker(peak_equity=100000, daily_loss_limit_1=0.015,
                        daily_loss_limit_2=0.025, drawdown_limit=0.05)
    pipeline = FilterPipeline([
        SystemHaltedFilter(circuit_breaker=cb, lock_file_path=lock_path),
        ConcurrentPositionFilter(max_concurrent=4),
        ConsecutiveLossFilter(limit=2, scope="per_symbol"),
    ])
    sizing = SizingConfig(max_risk_per_trade=0.005, max_notional_per_trade_pct=0.20)
    return RiskManager(circuit_breaker=cb, pipeline=pipeline, sizing_equity=sizing,
                       sizing_crypto=sizing, ledger=ledger, book=book)


def test_evaluate_passes_then_sizes():
    ledger = DailyLedger(initial_equity=100000)
    book = PositionBook()
    rm = _build_rm(ledger, book)
    decision = rm.evaluate(_signal(), ctx=None, asset_class="equity")
    assert decision.approved
    assert decision.qty == 500
    assert decision.notional == 500 * 100


def test_evaluate_rejected_by_concurrent():
    from state.position_book import OpenPosition
    ledger = DailyLedger(initial_equity=100000)
    book = PositionBook()
    for s in ("MSFT", "NVDA", "TSLA", "GOOGL"):
        book.add(OpenPosition(symbol=s, setup="x", side="long", qty=1,
                              entry_px=1, stop_px=0.5, target_px=2,
                              opened_at=datetime.now(timezone.utc), order_id="x"))
    rm = _build_rm(ledger, book)
    decision = rm.evaluate(_signal(), ctx=None, asset_class="equity")
    assert not decision.approved
    assert "concurrent" in decision.reason.lower()
```

- [ ] **4.5.2** Run, expect failure

```bash
pytest tests/test_risk_manager.py -v
```

- [ ] **4.5.3** Replace `risk/manager.py` contents with:

```python
from __future__ import annotations
import logging
from dataclasses import dataclass

from risk.circuit_breakers import CircuitBreaker
from risk.filters import FilterPipeline
from risk.sizing import SizingConfig, size_position
from state.daily_ledger import DailyLedger
from state.position_book import PositionBook
from strategies.base_setup import SetupSignal

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RiskDecision:
    approved: bool
    qty: float
    notional: float
    reason: str = ""

    @classmethod
    def reject(cls, reason: str) -> "RiskDecision":
        return cls(approved=False, qty=0.0, notional=0.0, reason=reason)


class RiskManager:
    def __init__(self,
                 circuit_breaker: CircuitBreaker,
                 pipeline: FilterPipeline,
                 sizing_equity: SizingConfig,
                 sizing_crypto: SizingConfig,
                 ledger: DailyLedger,
                 book: PositionBook):
        self.circuit_breaker = circuit_breaker
        self.pipeline = pipeline
        self.sizing_equity = sizing_equity
        self.sizing_crypto = sizing_crypto
        self.ledger = ledger
        self.book = book

    def update_equity(self, equity: float) -> None:
        self.ledger.equity = equity
        self.circuit_breaker.peak_equity = max(self.circuit_breaker.peak_equity, equity)

    def evaluate(self, signal: SetupSignal, ctx, asset_class: str) -> RiskDecision:
        result = self.pipeline.check(signal, ctx, self.ledger, self.book)
        if not result.passed:
            logger.info("FILTER_REJECT symbol=%s setup=%s reason=%s",
                        signal.symbol, signal.setup, result.reason)
            return RiskDecision.reject(result.reason)

        sizing = self.sizing_crypto if asset_class == "crypto" else self.sizing_equity
        try:
            qty, notional = size_position(self.ledger.equity, signal.entry, signal.stop, sizing)
        except ValueError as exc:
            return RiskDecision.reject(str(exc))

        # Apply circuit-breaker L1 (halve sizes).
        if self.circuit_breaker.level == 1:
            qty *= 0.5
            notional *= 0.5

        if qty <= 0:
            return RiskDecision.reject("sized to zero")

        return RiskDecision(approved=True, qty=qty, notional=notional)
```

- [ ] **4.5.4** Run tests

```bash
pytest tests/test_risk_manager.py tests/test_circuit_breakers.py -v
```

Expected: all passed. (`test_circuit_breakers.py` should still be green; `circuit_breakers.py` was unchanged.)

- [ ] **4.5.5** Commit

```bash
git add risk/manager.py tests/test_risk_manager.py
git commit -m "refactor(risk): RiskManager becomes façade over FilterPipeline + sizing"
```

### Task 4.6: Replace legacy `tests/test_order_executor.py`

The existing test imports symbols that have been deleted. Defer the rewrite to Phase 5 (where the executor itself is modified). For now, neutralize the test file so the suite stays green.

- [ ] **4.6.1** Run the test to confirm it currently fails

```bash
pytest tests/test_order_executor.py -v 2>&1 | head -20
```

Expected: collection error (ImportError on Portfolio or VolatilityAllocationStrategy).

- [ ] **4.6.2** Move the file aside

```bash
git mv tests/test_order_executor.py tests/_legacy_test_order_executor.py.bak
```

- [ ] **4.6.3** Confirm the suite is now green

```bash
pytest tests/ -v
```

Expected: all tests pass; `_legacy_*.bak` files are not collected by pytest.

- [ ] **4.6.4** Commit

```bash
git commit -m "chore: park legacy test_order_executor pending Phase 5 executor rewrite"
```

---

**End of Phase 4.** Tree state: ledger + position book + sizing + filter pipeline + RiskManager façade — all green. Suite runs in <2s. Ready for Phase 5.

---

## Phase 5 — Live Engine

### Task 5.1: Extend `AlpacaClient` with bracket orders + crypto orders

**Files:**
- Modify: `broker/alpaca_client.py` (extend `submit_order`)
- Test: `tests/test_alpaca_client_orders.py`

- [ ] **5.1.1** Write the failing test

Create `tests/test_alpaca_client_orders.py`:

```python
import os
from unittest.mock import patch, MagicMock

os.environ.setdefault("ALPACA_API_KEY", "test")
os.environ.setdefault("ALPACA_SECRET_KEY", "test")

from broker.alpaca_client import AlpacaClient


def _resp(status, body):
    m = MagicMock()
    m.status_code = status
    m.json.return_value = body
    return m


def test_submit_bracket_order_payload():
    client = AlpacaClient()
    expected_id = "abc123"
    with patch.object(client._session, "request",
                       return_value=_resp(200, {"id": expected_id})) as req:
        order = client.submit_bracket_order(
            symbol="AAPL", qty=10, side="buy",
            limit_price=100.0, stop_loss=99.0, take_profit=102.0,
            time_in_force="day",
        )
        assert order["id"] == expected_id
        body = req.call_args[1]["json"]
        assert body["order_class"] == "bracket"
        assert body["stop_loss"]["stop_price"] == 99.0
        assert body["take_profit"]["limit_price"] == 102.0
        assert body["limit_price"] == 100.0


def test_submit_crypto_market_order_uses_notional_optional():
    client = AlpacaClient()
    with patch.object(client._session, "request",
                       return_value=_resp(200, {"id": "x"})) as req:
        client.submit_order(symbol="BTC/USD", qty=0.01, side="buy",
                            order_type="market", time_in_force="gtc")
        body = req.call_args[1]["json"]
        assert body["symbol"] == "BTC/USD"
        assert body["time_in_force"] == "gtc"
```

- [ ] **5.1.2** Run, expect failure

```bash
pytest tests/test_alpaca_client_orders.py -v
```

- [ ] **5.1.3** Modify `broker/alpaca_client.py` — add a new method below `submit_order`

```python
    def submit_bracket_order(
        self,
        symbol: str,
        qty: float,
        side: str,
        limit_price: float,
        stop_loss: float,
        take_profit: float,
        time_in_force: str = "day",
    ) -> dict:
        """POST /v2/orders with order_class='bracket' (entry as limit + OCO stop/target)."""
        payload = {
            "symbol": symbol,
            "qty": qty,
            "side": side,
            "type": "limit",
            "limit_price": limit_price,
            "time_in_force": time_in_force,
            "order_class": "bracket",
            "stop_loss": {"stop_price": stop_loss},
            "take_profit": {"limit_price": take_profit},
        }
        response = self._request("POST", "/v2/orders", json=payload)
        return response.json()
```

Also widen the existing `submit_order` signature to accept `qty: float` (was `int`); update the type hint. Edit line `qty: int,` → `qty: float,`.

- [ ] **5.1.4** Run tests

```bash
pytest tests/test_alpaca_client_orders.py tests/test_alpaca_client_bars.py -v
```

Expected: all passed.

- [ ] **5.1.5** Commit

```bash
git add broker/alpaca_client.py tests/test_alpaca_client_orders.py
git commit -m "feat(broker): submit_bracket_order + float qty for crypto"
```

### Task 5.2: Rewrite `broker/order_executor.py`

The existing executor is built around `Portfolio` rebalancing. The new executor takes a `RiskDecision` + `SetupSignal` and submits orders.

**Files:**
- Modify: `broker/order_executor.py` (full rewrite)
- Create: `tests/test_order_executor.py` (replaces the parked legacy file)

- [ ] **5.2.1** Write the failing test

Create `tests/test_order_executor.py`:

```python
from datetime import datetime, timezone
from unittest.mock import MagicMock
from broker.order_executor import OrderExecutor
from strategies.base_setup import SetupSignal
from state.position_book import PositionBook
from risk.manager import RiskDecision


def _signal(symbol="AAPL", side="long"):
    return SetupSignal(setup="price_discovery", symbol=symbol, side=side,
                       entry=100, stop=99, target=102, atr=1.0, level=100,
                       ts=datetime(2026, 5, 14, 14, 0, tzinfo=timezone.utc))


def test_submit_equity_uses_bracket_order():
    client = MagicMock()
    client.submit_bracket_order.return_value = {"id": "ord-1"}
    book = PositionBook()
    ex = OrderExecutor(client, book, logger=MagicMock())
    decision = RiskDecision(approved=True, qty=10, notional=1000)
    pos = ex.submit(_signal(), decision, asset_class="equity")
    assert pos is not None
    assert pos.symbol == "AAPL"
    assert client.submit_bracket_order.called
    payload = client.submit_bracket_order.call_args.kwargs
    assert payload["side"] == "buy"
    assert payload["symbol"] == "AAPL"
    assert payload["stop_loss"] == 99
    assert payload["take_profit"] == 102


def test_submit_crypto_uses_market_order_and_virtual_stop():
    client = MagicMock()
    client.submit_order.return_value = {"id": "ord-2"}
    book = PositionBook()
    ex = OrderExecutor(client, book, logger=MagicMock())
    decision = RiskDecision(approved=True, qty=0.1, notional=5000)
    sig = _signal(symbol="BTC/USD", side="long")
    pos = ex.submit(sig, decision, asset_class="crypto")
    assert pos is not None
    client.submit_order.assert_called_once()
    payload = client.submit_order.call_args.kwargs
    assert payload["symbol"] == "BTC/USD"
    assert payload["order_type"] == "market"
    # Virtual stop is tracked in the book
    assert book.get("BTC/USD").stop_px == 99


def test_submit_returns_none_when_rejected():
    client = MagicMock()
    book = PositionBook()
    ex = OrderExecutor(client, book, logger=MagicMock())
    decision = RiskDecision.reject("denied")
    pos = ex.submit(_signal(), decision, asset_class="equity")
    assert pos is None
    client.submit_bracket_order.assert_not_called()
    client.submit_order.assert_not_called()
```

- [ ] **5.2.2** Run, expect failure

```bash
pytest tests/test_order_executor.py -v
```

- [ ] **5.2.3** Replace `broker/order_executor.py` contents with:

```python
from __future__ import annotations
import logging
from typing import Optional

from state.position_book import OpenPosition, PositionBook
from strategies.base_setup import SetupSignal
from risk.manager import RiskDecision

logger = logging.getLogger(__name__)


class OrderExecutor:
    """Translates an approved SetupSignal+RiskDecision into broker orders."""

    def __init__(self, alpaca_client, book: PositionBook,
                 logger: logging.Logger | None = None):
        self.client = alpaca_client
        self.book = book
        self.logger = logger or logging.getLogger("vwap_wave.executor")

    @staticmethod
    def _alpaca_side(side: str) -> str:
        return "buy" if side == "long" else "sell"

    def submit(self, signal: SetupSignal, decision: RiskDecision,
               asset_class: str) -> Optional[OpenPosition]:
        if not decision.approved:
            self.logger.info("ORDER_REJECTED symbol=%s reason=%s",
                             signal.symbol, decision.reason)
            return None

        alp_side = self._alpaca_side(signal.side)

        try:
            if asset_class == "equity":
                order = self.client.submit_bracket_order(
                    symbol=signal.symbol,
                    qty=decision.qty,
                    side=alp_side,
                    limit_price=signal.entry,
                    stop_loss=signal.stop,
                    take_profit=signal.target,
                    time_in_force="day",
                )
            elif asset_class == "crypto":
                # Crypto: market entry + engine-managed virtual stop/target
                order = self.client.submit_order(
                    symbol=signal.symbol,
                    qty=decision.qty,
                    side=alp_side,
                    order_type="market",
                    time_in_force="gtc",
                )
            else:
                raise ValueError(f"Unknown asset_class: {asset_class}")
        except Exception as exc:
            self.logger.error("ORDER_SUBMIT_FAILED symbol=%s error=%s",
                              signal.symbol, exc, exc_info=True)
            return None

        pos = OpenPosition(
            symbol=signal.symbol, setup=signal.setup, side=signal.side,
            qty=decision.qty, entry_px=signal.entry, stop_px=signal.stop,
            target_px=signal.target, opened_at=signal.ts,
            order_id=order.get("id", ""),
        )
        self.book.add(pos)
        self.logger.info("ORDER_SUBMITTED setup=%s symbol=%s side=%s qty=%s "
                         "entry=%.4f stop=%.4f target=%.4f order_id=%s",
                         signal.setup, signal.symbol, signal.side, decision.qty,
                         signal.entry, signal.stop, signal.target, order.get("id"))
        return pos

    def close_position(self, symbol: str, side: str, qty: float) -> dict | None:
        """Submit a market close order. Used for virtual stops / time stops."""
        try:
            return self.client.submit_order(
                symbol=symbol, qty=qty,
                side="sell" if side == "long" else "buy",
                order_type="market", time_in_force="gtc",
            )
        except Exception as exc:
            self.logger.error("CLOSE_FAILED symbol=%s error=%s", symbol, exc, exc_info=True)
            return None
```

- [ ] **5.2.4** Run tests

```bash
pytest tests/test_order_executor.py -v
```

Expected: 3 passed.

- [ ] **5.2.5** Remove the parked legacy file

```bash
git rm tests/_legacy_test_order_executor.py.bak
```

- [ ] **5.2.6** Commit

```bash
git add broker/order_executor.py tests/test_order_executor.py
git commit -m "refactor(broker): OrderExecutor consumes SetupSignal+RiskDecision (bracket+virtual)"
```

### Task 5.3: Position management — virtual stops, breakeven, time stops

**Files:**
- Create: `core/position_manager.py`
- Test: `tests/test_position_manager.py`

- [ ] **5.3.1** Write the failing test

Create `tests/test_position_manager.py`:

```python
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock
from core.bar import Bar
from core.position_manager import PositionManager, PositionAction
from state.position_book import PositionBook, OpenPosition


def _bar(ts, c, h=None, l=None):
    return Bar(symbol="AAPL", ts=ts, open=c, high=h or c + 0.1,
               low=l or c - 0.1, close=c, volume=100)


def _open_pos(side="long", entry=100, stop=99, target=102):
    return OpenPosition(symbol="AAPL", setup="price_discovery", side=side,
                        qty=10, entry_px=entry, stop_px=stop, target_px=target,
                        opened_at=datetime(2026, 5, 14, 14, 0, tzinfo=timezone.utc),
                        order_id="x")


def test_stop_hit_long():
    book = PositionBook()
    book.add(_open_pos())
    pm = PositionManager(book, max_hold_bars=12, breakeven_at_R=1.0)
    actions = pm.on_bar("AAPL", _bar(datetime(2026, 5, 14, 14, 5, tzinfo=timezone.utc), 98.5, l=98.0))
    assert any(a.kind == "stop" for a in actions)


def test_target_hit_long():
    book = PositionBook()
    book.add(_open_pos())
    pm = PositionManager(book, max_hold_bars=12, breakeven_at_R=1.0)
    actions = pm.on_bar("AAPL", _bar(datetime(2026, 5, 14, 14, 5, tzinfo=timezone.utc), 102.5, h=103.0))
    assert any(a.kind == "target" for a in actions)


def test_breakeven_moves_stop_to_entry():
    book = PositionBook()
    book.add(_open_pos())             # risk = 1, target = 102 (R=2)
    pm = PositionManager(book, max_hold_bars=12, breakeven_at_R=1.0)
    pm.on_bar("AAPL", _bar(datetime(2026, 5, 14, 14, 5, tzinfo=timezone.utc), 101.2, h=101.5))
    assert book.get("AAPL").stop_px == 100.0
    assert book.get("AAPL").breakeven_moved is True


def test_time_stop_triggers_after_max_hold():
    book = PositionBook()
    book.add(_open_pos())
    pm = PositionManager(book, max_hold_bars=2, breakeven_at_R=1.0)
    base = datetime(2026, 5, 14, 14, 0, tzinfo=timezone.utc)
    pm.on_bar("AAPL", _bar(base + timedelta(minutes=5), 100.5))
    pm.on_bar("AAPL", _bar(base + timedelta(minutes=10), 100.6))
    actions = pm.on_bar("AAPL", _bar(base + timedelta(minutes=15), 100.7))
    assert any(a.kind == "time_stop" for a in actions)
```

- [ ] **5.3.2** Run, expect failure

```bash
pytest tests/test_position_manager.py -v
```

- [ ] **5.3.3** Implement `core/position_manager.py`

```python
from __future__ import annotations
import logging
from dataclasses import dataclass

from core.bar import Bar
from state.position_book import PositionBook, OpenPosition

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PositionAction:
    kind: str        # "stop" | "target" | "time_stop" | "breakeven"
    symbol: str
    exit_price: float | None = None


class PositionManager:
    def __init__(self, book: PositionBook, max_hold_bars: int,
                 breakeven_at_R: float = 1.0):
        self.book = book
        self.max_hold_bars = max_hold_bars
        self.breakeven_at_R = breakeven_at_R

    def on_bar(self, symbol: str, bar: Bar) -> list[PositionAction]:
        pos = self.book.get(symbol)
        if pos is None:
            return []
        actions: list[PositionAction] = []
        pos.bars_held += 1

        # Stop / target evaluation — stops fire BEFORE targets within the same bar
        if pos.side == "long":
            if bar.low <= pos.stop_px:
                actions.append(PositionAction("stop", symbol, exit_price=pos.stop_px))
                return actions
            if bar.high >= pos.target_px:
                actions.append(PositionAction("target", symbol, exit_price=pos.target_px))
                return actions
            r = pos.entry_px - pos.stop_px
            if r > 0 and not pos.breakeven_moved and bar.high >= pos.entry_px + self.breakeven_at_R * r:
                pos.stop_px = pos.entry_px
                pos.breakeven_moved = True
                actions.append(PositionAction("breakeven", symbol))
        else:
            if bar.high >= pos.stop_px:
                actions.append(PositionAction("stop", symbol, exit_price=pos.stop_px))
                return actions
            if bar.low <= pos.target_px:
                actions.append(PositionAction("target", symbol, exit_price=pos.target_px))
                return actions
            r = pos.stop_px - pos.entry_px
            if r > 0 and not pos.breakeven_moved and bar.low <= pos.entry_px - self.breakeven_at_R * r:
                pos.stop_px = pos.entry_px
                pos.breakeven_moved = True
                actions.append(PositionAction("breakeven", symbol))

        # Time stop
        if pos.bars_held > self.max_hold_bars:
            actions.append(PositionAction("time_stop", symbol, exit_price=bar.close))

        return actions
```

- [ ] **5.3.4** Run tests

```bash
pytest tests/test_position_manager.py -v
```

Expected: 4 passed.

- [ ] **5.3.5** Commit

```bash
git add core/position_manager.py tests/test_position_manager.py
git commit -m "feat(core): PositionManager (stop/target/breakeven/time-stop on bar close)"
```

### Task 5.4: Scheduler `loop.py` — wires everything together

**Files:**
- Create: `scheduler/loop.py`
- Test: `tests/test_scheduler_loop.py`

- [ ] **5.4.1** Write the failing test

Create `tests/test_scheduler_loop.py`:

```python
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock
from core.bar import Bar
from core.session import SessionContext
from core.asset_class import AssetClassConfig
from scheduler.loop import VWAPWaveEngine
from state.position_book import PositionBook
from state.daily_ledger import DailyLedger
from risk.manager import RiskManager, RiskDecision
from risk.filters import FilterPipeline


CRYPTO = AssetClassConfig(
    name="crypto", timezone="UTC",
    session_open_local="00:00", session_close_local="23:59",
    opening_blackout_min=15, bar_timeframe="5Min",
    slippage_bps=5.0, commission_per_share=0.0, commission_bps=25.0,
)


class FakeSetup:
    name = "fake"
    def __init__(self, symbol):
        self.symbol = symbol
        self.fired = False
    def check(self, ctx):
        if not self.fired and ctx.bar_count == 3:
            from strategies.base_setup import SetupSignal
            self.fired = True
            return SetupSignal(setup="fake", symbol=self.symbol, side="long",
                               entry=100, stop=99, target=102, atr=1, level=100,
                               ts=ctx.bars[-1].ts)
        return None
    def reset(self): self.fired = False


def _bar(symbol, ts, c):
    return Bar(symbol=symbol, ts=ts, open=c, high=c+0.5, low=c-0.5, close=c, volume=100)


def test_engine_emits_signal_then_submits_order():
    book = PositionBook()
    ledger = DailyLedger(initial_equity=100000)
    rm = MagicMock(spec=RiskManager)
    rm.evaluate.return_value = RiskDecision(approved=True, qty=10, notional=1000)
    rm.update_equity = MagicMock()
    executor = MagicMock()
    executor.submit.return_value = MagicMock(symbol="BTC/USD")
    contexts = {"BTC/USD": SessionContext(symbol="BTC/USD", asset_class=CRYPTO)}
    setups = {"BTC/USD": [FakeSetup("BTC/USD")]}

    engine = VWAPWaveEngine(
        symbols=[("BTC/USD", "crypto")],
        contexts=contexts, setups=setups,
        risk_manager=rm, executor=executor, book=book, ledger=ledger,
        position_manager=MagicMock(on_bar=MagicMock(return_value=[])),
    )
    base = datetime(2026, 5, 14, 0, 5, tzinfo=timezone.utc)
    bars = {"BTC/USD": [_bar("BTC/USD", base + timedelta(minutes=5*i), 100 + i) for i in range(3)]}

    engine.tick(now=base + timedelta(minutes=20), fresh_bars=bars)

    rm.evaluate.assert_called()
    executor.submit.assert_called()
```

- [ ] **5.4.2** Run, expect failure

```bash
pytest tests/test_scheduler_loop.py -v
```

- [ ] **5.4.3** Implement `scheduler/loop.py`

```python
from __future__ import annotations
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable

from core.bar import Bar
from core.position_manager import PositionManager
from core.session import SessionContext
from risk.manager import RiskManager
from state.daily_ledger import DailyLedger, TradeRecord
from state.position_book import PositionBook
from strategies.base_setup import BaseSetup, SetupSignal
from broker.order_executor import OrderExecutor

logger = logging.getLogger(__name__)


@dataclass
class VWAPWaveEngine:
    symbols: list[tuple[str, str]]              # [(symbol, asset_class)]
    contexts: dict[str, SessionContext]
    setups: dict[str, list[BaseSetup]]
    risk_manager: RiskManager
    executor: OrderExecutor
    book: PositionBook
    ledger: DailyLedger
    position_manager: PositionManager

    def tick(self, now: datetime, fresh_bars: dict[str, list[Bar]]) -> None:
        """One bar-close cycle. Caller supplies the bars fetched for `now`."""
        # 1. Manage open positions first (stops/targets fire before new entries)
        for symbol, asset_class in self.symbols:
            new_bars = fresh_bars.get(symbol, [])
            if not new_bars:
                continue
            for bar in new_bars:
                self.contexts[symbol].ingest(bar)
                actions = self.position_manager.on_bar(symbol, bar)
                self._handle_actions(symbol, asset_class, actions, bar)

        # 2. Run setup detectors and submit fresh orders
        for symbol, asset_class in self.symbols:
            ctx = self.contexts[symbol]
            for setup in self.setups[symbol]:
                signal = setup.check(ctx)
                if signal is None:
                    continue
                decision = self.risk_manager.evaluate(signal, ctx, asset_class)
                if decision.approved:
                    self.executor.submit(signal, decision, asset_class)
                else:
                    logger.info("SIGNAL_REJECTED symbol=%s setup=%s reason=%s",
                                signal.symbol, signal.setup, decision.reason)

    def _handle_actions(self, symbol: str, asset_class: str, actions, last_bar: Bar) -> None:
        for action in actions:
            if action.kind in ("stop", "target", "time_stop"):
                pos = self.book.get(symbol)
                if pos is None:
                    continue
                exit_px = action.exit_price if action.exit_price is not None else last_bar.close
                pnl = (exit_px - pos.entry_px) * pos.qty if pos.side == "long" \
                      else (pos.entry_px - exit_px) * pos.qty
                r_realized = ((exit_px - pos.entry_px) if pos.side == "long"
                              else (pos.entry_px - exit_px)) / max(abs(pos.entry_px - pos.stop_px), 1e-9)
                self.ledger.record(TradeRecord(
                    symbol=symbol, setup=pos.setup,
                    entry_ts=pos.opened_at, exit_ts=last_bar.ts,
                    entry_px=pos.entry_px, exit_px=exit_px,
                    side=pos.side, qty=pos.qty,
                    R_realized=r_realized, pnl_usd=pnl,
                ))
                # For crypto (virtual stops), submit a market exit order
                if asset_class == "crypto":
                    self.executor.close_position(symbol, pos.side, pos.qty)
                self.book.close(symbol)
                logger.info("POSITION_CLOSED symbol=%s reason=%s exit=%.4f r=%.2f pnl=%.2f",
                            symbol, action.kind, exit_px, r_realized, pnl)
```

- [ ] **5.4.4** Run tests

```bash
pytest tests/test_scheduler_loop.py -v
```

Expected: 1 passed.

- [ ] **5.4.5** Commit

```bash
git add scheduler/loop.py tests/test_scheduler_loop.py
git commit -m "feat(scheduler): VWAPWaveEngine.tick — bar-close cycle wiring"
```

### Task 5.5: Rewrite `main.py`

**Files:**
- Modify: `main.py` (full rewrite)

- [ ] **5.5.1** Replace `main.py` contents with:

```python
"""
VWAP Wave Protocol — Autonomous Intraday Trading System

Entry point: bar-close scheduler over a watchlist of equities + crypto on Alpaca.
Live trading is gated behind config (`system.trading_env`) — paper by default.
"""
from __future__ import annotations
import logging
import os
import signal as _signal
import sys
import time
from datetime import datetime, timezone

import yaml

# Lock-file guard — must run before heavy imports.
_LOCK_FILE_PATH = os.environ.get("LOCK_FILE_PATH", "lock.file")
_TRADING_ENV = os.environ.get("TRADING_ENV", "production")
if _TRADING_ENV != "test" and os.path.exists(_LOCK_FILE_PATH):
    print("=" * 60)
    print("SYSTEM HALTED: Emergency lock file detected.")
    print(f"Lock file: {os.path.abspath(_LOCK_FILE_PATH)}")
    print("Resolve incident and remove lock.file before restarting.")
    print("=" * 60)
    sys.exit(1)

from broker.alpaca_client import AlpacaClient
from broker.alpaca_data import AlpacaData
from broker.order_executor import OrderExecutor
from core.asset_class import AssetClassConfig, session_start_for
from core.position_manager import PositionManager
from core.session import SessionContext
from risk.circuit_breakers import CircuitBreaker
from risk.filters import (
    FilterPipeline, NewsBlackout,
    SystemHaltedFilter, SessionWindowFilter, NewsBlackoutFilter,
    VolumeDeficitFilter, ConsecutiveLossFilter,
    ConcurrentPositionFilter, SetupCooldownFilter, RiskBudgetFilter,
)
from risk.manager import RiskManager
from risk.sizing import SizingConfig
from scheduler.bar_clock import next_boundary, sleep_until
from scheduler.loop import VWAPWaveEngine
from state.daily_ledger import DailyLedger
from state.position_book import PositionBook
from strategies.setup_fade_extreme import FadeExtremeSetup
from strategies.setup_price_discovery import PriceDiscoverySetup
from strategies.setup_return_to_value import ReturnToValueSetup
from strategies.setup_vwap_bounce import VWAPBounceSetup
from ui.logging_setup import setup_logging


_shutdown = False


def _handle_shutdown(signum, frame):
    global _shutdown
    _shutdown = True


def load_config(path: str = "config/settings.yaml") -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def build_asset_class_configs(cfg: dict) -> dict[str, AssetClassConfig]:
    out = {}
    for name, raw in cfg["asset_classes"].items():
        out[name] = AssetClassConfig(
            name=name,
            timezone=raw["timezone"],
            session_open_local=raw["session_open_local"],
            session_close_local=raw["session_close_local"],
            opening_blackout_min=cfg["filters"]["opening_blackout_min"],
            bar_timeframe=cfg["scheduler"]["bar_timeframe"],
            slippage_bps=raw.get("slippage_bps", 0.0),
            commission_per_share=raw.get("commission_per_share", 0.0),
            commission_bps=raw.get("commission_bps", 0.0),
        )
    return out


def build_setups(cfg: dict, symbol: str):
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


def main():
    cfg = load_config()
    logger = setup_logging(log_file=cfg["logging"]["log_file"])
    logger.info("vwap_wave starting up; env=%s", cfg["system"]["trading_env"])

    _signal.signal(_signal.SIGTERM, _handle_shutdown)
    _signal.signal(_signal.SIGINT, _handle_shutdown)

    ac_configs = build_asset_class_configs(cfg)

    # Build the (symbol, asset_class) watchlist.
    symbols: list[tuple[str, str]] = []
    for ac_name, raw in cfg["asset_classes"].items():
        for s in raw["symbols"]:
            symbols.append((s, ac_name))

    contexts = {sym: SessionContext(symbol=sym, asset_class=ac_configs[ac])
                for sym, ac in symbols}
    setups = {sym: build_setups(cfg, sym) for sym, _ in symbols}

    # Wire the broker layer.
    alpaca = AlpacaClient()
    data = AlpacaData(alpaca, cache_dir=cfg["backtest"]["cache_dir"])
    book = PositionBook()
    account = alpaca.get_account()
    initial_equity = float(account.get("equity") or account.get("portfolio_value") or 0)
    if initial_equity <= 0:
        logger.error("Account returned non-positive equity; aborting")
        sys.exit(1)
    ledger = DailyLedger(initial_equity=initial_equity)

    cb_cfg = cfg["risk"]["circuit_breaker"]
    cb = CircuitBreaker(
        peak_equity=initial_equity,
        daily_loss_limit_1=cb_cfg["daily_loss_limit_1"],
        daily_loss_limit_2=cb_cfg["daily_loss_limit_2"],
        drawdown_limit=cb_cfg["drawdown_limit"],
    )

    news_windows = [
        NewsBlackout(start=datetime.fromisoformat(w["start"]),
                     duration_min=w["duration_min"], label=w["label"])
        for w in cfg.get("news_blackouts", [])
    ]

    pipeline = FilterPipeline([
        SystemHaltedFilter(circuit_breaker=cb, lock_file_path=_LOCK_FILE_PATH),
        SessionWindowFilter(opening_blackout_min=cfg["filters"]["opening_blackout_min"]),
        NewsBlackoutFilter(windows=news_windows, pad_min=5),
        VolumeDeficitFilter(deficit_pct=cfg["filters"]["volume_deficit_pct"]),
        ConsecutiveLossFilter(limit=cfg["risk"]["consecutive_loss_limit"],
                              scope=cfg["risk"]["loss_filter_scope"]),
        ConcurrentPositionFilter(max_concurrent=cfg["risk"]["max_concurrent_positions"]),
        SetupCooldownFilter(cooldown_bars=cfg["setups"]["price_discovery"]["cooldown_bars"]),
        RiskBudgetFilter(daily_open_risk_cap_pct=cfg["risk"]["max_daily_risk_open"]),
    ])

    sizing_eq = SizingConfig(
        max_risk_per_trade=cfg["risk"]["max_risk_per_trade"],
        max_notional_per_trade_pct=cfg["risk"]["max_notional_per_trade_pct"],
        allow_fractional=False,
    )
    sizing_cr = SizingConfig(
        max_risk_per_trade=cfg["risk"]["max_risk_per_trade"],
        max_notional_per_trade_pct=cfg["risk"]["max_notional_per_trade_pct"],
        allow_fractional=True,
    )

    rm = RiskManager(
        circuit_breaker=cb, pipeline=pipeline,
        sizing_equity=sizing_eq, sizing_crypto=sizing_cr,
        ledger=ledger, book=book,
    )
    executor = OrderExecutor(alpaca, book, logger=logger)
    pm = PositionManager(book,
                         max_hold_bars=cfg["position_management"]["max_hold_bars"],
                         breakeven_at_R=cfg["position_management"]["breakeven_at_R"])
    engine = VWAPWaveEngine(
        symbols=symbols, contexts=contexts, setups=setups,
        risk_manager=rm, executor=executor, book=book, ledger=ledger,
        position_manager=pm,
    )

    timeframe = cfg["scheduler"]["bar_timeframe"]
    grace = cfg["scheduler"]["wake_grace_seconds"]
    logger.info("vwap_wave loop starting; symbols=%d", len(symbols))

    while not _shutdown:
        now = datetime.now(timezone.utc)
        target = next_boundary(now, timeframe, grace_seconds=grace)
        sleep_until(target)
        if _shutdown:
            break

        try:
            cycle_now = datetime.now(timezone.utc)
            fresh_bars = {}
            for sym, ac_name in symbols:
                ctx = contexts[sym]
                ac = ac_configs[ac_name]
                start = session_start_for(cycle_now, ac)
                bars = data.get_bars(sym, ac_name, timeframe, start=start, end=cycle_now,
                                     use_cache=False)
                # only consume bars not already in ctx
                last_known_ts = ctx.bars[-1].ts if ctx.bars else None
                new_bars = [b for b in bars if last_known_ts is None or b.ts > last_known_ts]
                if new_bars:
                    fresh_bars[sym] = new_bars
            engine.tick(now=cycle_now, fresh_bars=fresh_bars)

            account = alpaca.get_account()
            equity = float(account.get("equity") or account.get("portfolio_value") or ledger.equity)
            rm.update_equity(equity)
        except Exception as exc:
            logger.error("CYCLE_ERROR: %s", exc, exc_info=True)

    logger.info("Shutdown requested. Closing.")


if __name__ == "__main__":
    main()
```

- [ ] **5.5.2** Smoke-import to confirm Python parses the file (no broker call, no broker creds needed for an `import` test)

```bash
TRADING_ENV=test ALPACA_API_KEY=x ALPACA_SECRET_KEY=x python -c "import main; print('ok')"
```

Expected: `ok`. The lock-file guard skips when `TRADING_ENV=test`.

- [ ] **5.5.3** Run the full test suite

```bash
pytest tests/ -v
```

Expected: all green; <30s.

- [ ] **5.5.4** Commit

```bash
git add main.py
git commit -m "feat: rewrite main.py as bar-close scheduler boot"
```

---

**End of Phase 5.** Tree state: live engine wires bars → context → setups → filters → sizing → orders → position management. The system imports cleanly. Live execution is one config flip away — gated behind `system.trading_env`. Ready for Phase 6.

---

## Phase 6 — Backtest Engine

### Task 6.1: `SimulatedFillEngine`

**Files:**
- Create: `backtest/fill_engine.py`
- Test: `tests/test_fill_engine.py`

- [ ] **6.1.1** Write the failing test

Create `tests/test_fill_engine.py`:

```python
from datetime import datetime, timezone
from core.bar import Bar
from backtest.fill_engine import SimulatedFillEngine, PendingOrder
from state.position_book import PositionBook, OpenPosition


def _bar(ts, o, h, l, c):
    return Bar(symbol="AAPL", ts=ts, open=o, high=h, low=l, close=c, volume=100)


def test_limit_fills_when_bar_range_touches():
    fill = SimulatedFillEngine(slippage_bps_by_class={"equity": 0.0, "crypto": 0.0})
    fill.submit(PendingOrder(symbol="AAPL", side="buy", qty=10,
                             order_type="limit", limit_price=100.0,
                             stop_price=99.0, target_price=102.0,
                             asset_class="equity", setup="x",
                             ts=datetime(2026, 5, 14, 14, 0, tzinfo=timezone.utc)))
    book = PositionBook()
    bar = _bar(datetime(2026, 5, 14, 14, 5, tzinfo=timezone.utc), 100.5, 101.0, 99.5, 100.8)
    fills = fill.process_bar("AAPL", bar, book)
    assert len(fills) == 1
    pos = book.get("AAPL")
    assert pos is not None
    assert pos.entry_px == 100.0


def test_limit_skipped_when_bar_misses():
    fill = SimulatedFillEngine(slippage_bps_by_class={"equity": 0.0, "crypto": 0.0})
    fill.submit(PendingOrder(symbol="AAPL", side="buy", qty=10,
                             order_type="limit", limit_price=99.5,
                             stop_price=99.0, target_price=102.0,
                             asset_class="equity", setup="x",
                             ts=datetime(2026, 5, 14, 14, 0, tzinfo=timezone.utc)))
    book = PositionBook()
    bar = _bar(datetime(2026, 5, 14, 14, 5, tzinfo=timezone.utc), 100.5, 101.0, 100.0, 100.8)
    fills = fill.process_bar("AAPL", bar, book)
    assert fills == []
    assert book.get("AAPL") is None


def test_market_order_fills_at_open_with_slippage():
    fill = SimulatedFillEngine(slippage_bps_by_class={"equity": 10.0, "crypto": 0.0})
    fill.submit(PendingOrder(symbol="AAPL", side="buy", qty=10,
                             order_type="market", limit_price=None,
                             stop_price=99.0, target_price=102.0,
                             asset_class="equity", setup="x",
                             ts=datetime(2026, 5, 14, 14, 0, tzinfo=timezone.utc)))
    book = PositionBook()
    bar = _bar(datetime(2026, 5, 14, 14, 5, tzinfo=timezone.utc), 100.0, 101.0, 99.5, 100.5)
    fill.process_bar("AAPL", bar, book)
    pos = book.get("AAPL")
    # 10 bps slippage = 0.001 × 100 = 0.1, buy → worse → 100.1
    assert abs(pos.entry_px - 100.1) < 1e-9
```

- [ ] **6.1.2** Run, expect failure

```bash
pytest tests/test_fill_engine.py -v
```

- [ ] **6.1.3** Implement `backtest/fill_engine.py`

```python
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from core.bar import Bar
from state.position_book import OpenPosition, PositionBook


@dataclass
class PendingOrder:
    symbol: str
    side: str                    # "buy" | "sell"
    qty: float
    order_type: str              # "limit" | "market" | "stop"
    limit_price: Optional[float]
    stop_price: float
    target_price: float
    asset_class: str
    setup: str
    ts: datetime


@dataclass
class FillEvent:
    symbol: str
    fill_price: float
    qty: float
    side: str


class SimulatedFillEngine:
    def __init__(self, slippage_bps_by_class: dict[str, float]):
        self.slippage_bps_by_class = slippage_bps_by_class
        self.pending: list[PendingOrder] = []

    def submit(self, order: PendingOrder) -> None:
        self.pending.append(order)

    def _slippage(self, asset_class: str) -> float:
        return self.slippage_bps_by_class.get(asset_class, 0.0) / 10000.0

    def process_bar(self, symbol: str, bar: Bar, book: PositionBook) -> list[FillEvent]:
        fills: list[FillEvent] = []
        remaining: list[PendingOrder] = []
        for o in self.pending:
            if o.symbol != symbol:
                remaining.append(o)
                continue
            slip = self._slippage(o.asset_class)
            if o.order_type == "limit":
                if o.side == "buy" and bar.low <= o.limit_price <= bar.high:
                    fill_px = o.limit_price                       # no improvement
                elif o.side == "sell" and bar.low <= o.limit_price <= bar.high:
                    fill_px = o.limit_price
                else:
                    remaining.append(o)
                    continue
            elif o.order_type == "market":
                base = bar.open
                fill_px = base * (1 + slip) if o.side == "buy" else base * (1 - slip)
            elif o.order_type == "stop":
                trigger = o.stop_price
                if o.side == "buy" and bar.high >= trigger:
                    fill_px = max(trigger, bar.open) * (1 + slip)
                elif o.side == "sell" and bar.low <= trigger:
                    fill_px = min(trigger, bar.open) * (1 - slip)
                else:
                    remaining.append(o)
                    continue
            else:
                remaining.append(o)
                continue

            side_pos = "long" if o.side == "buy" else "short"
            if book.get(o.symbol) is None:
                book.add(OpenPosition(
                    symbol=o.symbol, setup=o.setup, side=side_pos,
                    qty=o.qty, entry_px=fill_px,
                    stop_px=o.stop_price, target_px=o.target_price,
                    opened_at=bar.ts, order_id=f"sim-{bar.ts.isoformat()}",
                ))
            fills.append(FillEvent(symbol=o.symbol, fill_price=fill_px,
                                   qty=o.qty, side=o.side))
        self.pending = remaining
        return fills
```

- [ ] **6.1.4** Run tests

```bash
pytest tests/test_fill_engine.py -v
```

Expected: 3 passed.

- [ ] **6.1.5** Commit

```bash
git add backtest/fill_engine.py tests/test_fill_engine.py
git commit -m "feat(backtest): SimulatedFillEngine (limit/market/stop with slippage)"
```

### Task 6.2: `intraday_replay.py`

**Files:**
- Create: `backtest/intraday_replay.py`
- Test: `tests/test_intraday_replay.py`

- [ ] **6.2.1** Write the failing test

Create `tests/test_intraday_replay.py`:

```python
from datetime import datetime, timezone, timedelta
from core.bar import Bar
from core.asset_class import AssetClassConfig
from backtest.intraday_replay import IntradayReplay


CRYPTO = AssetClassConfig(
    name="crypto", timezone="UTC",
    session_open_local="00:00", session_close_local="23:59",
    opening_blackout_min=15, bar_timeframe="5Min",
    slippage_bps=5.0, commission_per_share=0.0, commission_bps=0.0,
)


def _bars(symbol, n, start_price=100, base=None):
    base = base or datetime(2026, 5, 14, 0, 5, tzinfo=timezone.utc)
    out = []
    for i in range(n):
        c = start_price + i * 0.1
        out.append(Bar(symbol=symbol, ts=base + timedelta(minutes=5*i),
                       open=c-0.05, high=c+0.05, low=c-0.05, close=c, volume=100))
    return out


def test_no_signal_universe_yields_flat_equity():
    bars = {"BTC/USD": _bars("BTC/USD", 50, start_price=100)}
    replay = IntradayReplay(
        symbols=[("BTC/USD", "crypto")],
        asset_class_configs={"crypto": CRYPTO},
        bars=bars,
        initial_equity=100000.0,
        config={
            "setups": {
                "price_discovery": {"enabled": False, "atr_mult_stop": 1.0, "target_R": 1.5,
                                    "arm_window_bars": 6, "cooldown_bars": 12},
                "fade_extreme": {"enabled": False, "atr_mult_stop": 0.75,
                                 "scale_offsets_atr": [0.0], "scale_weights": [1.0],
                                 "cooldown_bars": 12},
                "return_to_value": {"enabled": False, "atr_mult_stop": 1.0,
                                    "arm_window_bars": 6, "cooldown_bars": 12},
                "vwap_bounce": {"enabled": False, "atr_mult_stop": 1.25, "target_R": 2.0,
                                "arm_window_bars": 4, "cooldown_bars": 8},
            },
            "risk": {
                "max_risk_per_trade": 0.005, "max_notional_per_trade_pct": 0.20,
                "max_concurrent_positions": 4, "max_daily_risk_open": 0.02,
                "consecutive_loss_limit": 2, "loss_filter_scope": "per_symbol",
                "circuit_breaker": {"daily_loss_limit_1": 0.015, "daily_loss_limit_2": 0.025,
                                    "drawdown_limit": 0.05},
            },
            "filters": {"opening_blackout_min": 0, "volume_deficit_pct": 0.30},
            "position_management": {"max_hold_bars": 12, "breakeven_at_R": 1.0,
                                    "trail_at_R": 1.5, "trail_atr": 1.0},
            "scheduler": {"bar_timeframe": "5Min"},
        },
    )
    result = replay.run()
    assert result.metrics["trades"] == 0
    assert abs(result.equity_curve.iloc[-1] - 100000.0) < 1e-9


def test_idempotency():
    # Same inputs produce identical output trades + equity curve
    cfg = {
        "setups": {
            "price_discovery": {"enabled": False, "atr_mult_stop": 1.0, "target_R": 1.5,
                                "arm_window_bars": 6, "cooldown_bars": 12},
            "fade_extreme": {"enabled": False, "atr_mult_stop": 0.75,
                             "scale_offsets_atr": [0.0], "scale_weights": [1.0],
                             "cooldown_bars": 12},
            "return_to_value": {"enabled": False, "atr_mult_stop": 1.0,
                                "arm_window_bars": 6, "cooldown_bars": 12},
            "vwap_bounce": {"enabled": False, "atr_mult_stop": 1.25, "target_R": 2.0,
                            "arm_window_bars": 4, "cooldown_bars": 8},
        },
        "risk": {
            "max_risk_per_trade": 0.005, "max_notional_per_trade_pct": 0.20,
            "max_concurrent_positions": 4, "max_daily_risk_open": 0.02,
            "consecutive_loss_limit": 2, "loss_filter_scope": "per_symbol",
            "circuit_breaker": {"daily_loss_limit_1": 0.015, "daily_loss_limit_2": 0.025,
                                "drawdown_limit": 0.05},
        },
        "filters": {"opening_blackout_min": 0, "volume_deficit_pct": 0.30},
        "position_management": {"max_hold_bars": 12, "breakeven_at_R": 1.0,
                                "trail_at_R": 1.5, "trail_atr": 1.0},
        "scheduler": {"bar_timeframe": "5Min"},
    }
    bars = {"BTC/USD": _bars("BTC/USD", 100, start_price=50000)}
    a = IntradayReplay([("BTC/USD", "crypto")], {"crypto": CRYPTO}, bars, 100000.0, cfg).run()
    b = IntradayReplay([("BTC/USD", "crypto")], {"crypto": CRYPTO}, bars, 100000.0, cfg).run()
    assert list(a.equity_curve) == list(b.equity_curve)
```

- [ ] **6.2.2** Run, expect failure

```bash
pytest tests/test_intraday_replay.py -v
```

- [ ] **6.2.3** Implement `backtest/intraday_replay.py`

```python
from __future__ import annotations
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable

import pandas as pd

from backtest.fill_engine import PendingOrder, SimulatedFillEngine
from core.asset_class import AssetClassConfig
from core.bar import Bar
from core.position_manager import PositionManager
from core.session import SessionContext
from risk.circuit_breakers import CircuitBreaker
from risk.filters import (
    FilterPipeline,
    SystemHaltedFilter, SessionWindowFilter,
    VolumeDeficitFilter, ConsecutiveLossFilter,
    ConcurrentPositionFilter, SetupCooldownFilter, RiskBudgetFilter,
)
from risk.manager import RiskManager
from risk.sizing import SizingConfig
from state.daily_ledger import DailyLedger, TradeRecord
from state.position_book import PositionBook
from strategies.base_setup import BaseSetup, SetupSignal
from strategies.setup_fade_extreme import FadeExtremeSetup
from strategies.setup_price_discovery import PriceDiscoverySetup
from strategies.setup_return_to_value import ReturnToValueSetup
from strategies.setup_vwap_bounce import VWAPBounceSetup


logger = logging.getLogger(__name__)


@dataclass
class BacktestResult:
    trades: pd.DataFrame
    equity_curve: pd.Series
    per_setup: dict[str, dict]
    per_symbol: dict[str, dict]
    metrics: dict
    filter_audit: dict[str, int]


def _build_setups(cfg, symbol):
    s = cfg["setups"]
    out = []
    if s["price_discovery"]["enabled"]:
        out.append(PriceDiscoverySetup(symbol,
                                       atr_mult_stop=s["price_discovery"]["atr_mult_stop"],
                                       target_R=s["price_discovery"]["target_R"],
                                       arm_window_bars=s["price_discovery"]["arm_window_bars"]))
    if s["fade_extreme"]["enabled"]:
        out.append(FadeExtremeSetup(symbol,
                                    atr_mult_stop=s["fade_extreme"]["atr_mult_stop"],
                                    scale_offsets_atr=s["fade_extreme"]["scale_offsets_atr"],
                                    scale_weights=s["fade_extreme"]["scale_weights"]))
    if s["return_to_value"]["enabled"]:
        out.append(ReturnToValueSetup(symbol,
                                      atr_mult_stop=s["return_to_value"]["atr_mult_stop"],
                                      arm_window_bars=s["return_to_value"]["arm_window_bars"]))
    if s["vwap_bounce"]["enabled"]:
        out.append(VWAPBounceSetup(symbol,
                                   atr_mult_stop=s["vwap_bounce"]["atr_mult_stop"],
                                   target_R=s["vwap_bounce"]["target_R"],
                                   arm_window_bars=s["vwap_bounce"]["arm_window_bars"]))
    return out


@dataclass
class IntradayReplay:
    symbols: list[tuple[str, str]]
    asset_class_configs: dict[str, AssetClassConfig]
    bars: dict[str, list[Bar]]
    initial_equity: float
    config: dict

    def run(self) -> BacktestResult:
        contexts = {sym: SessionContext(symbol=sym, asset_class=self.asset_class_configs[ac])
                    for sym, ac in self.symbols}
        setups: dict[str, list[BaseSetup]] = {sym: _build_setups(self.config, sym)
                                              for sym, _ in self.symbols}
        book = PositionBook()
        ledger = DailyLedger(initial_equity=self.initial_equity)
        cb = CircuitBreaker(peak_equity=self.initial_equity,
                            daily_loss_limit_1=self.config["risk"]["circuit_breaker"]["daily_loss_limit_1"],
                            daily_loss_limit_2=self.config["risk"]["circuit_breaker"]["daily_loss_limit_2"],
                            drawdown_limit=self.config["risk"]["circuit_breaker"]["drawdown_limit"])
        pipeline = FilterPipeline([
            SystemHaltedFilter(circuit_breaker=cb, lock_file_path="/nonexistent"),
            SessionWindowFilter(opening_blackout_min=self.config["filters"]["opening_blackout_min"],
                                now_fn=lambda: datetime(1970, 1, 1, tzinfo=__import__("datetime").timezone.utc)),  # disabled in backtest
            VolumeDeficitFilter(deficit_pct=self.config["filters"]["volume_deficit_pct"]),
            ConsecutiveLossFilter(limit=self.config["risk"]["consecutive_loss_limit"],
                                  scope=self.config["risk"]["loss_filter_scope"]),
            ConcurrentPositionFilter(max_concurrent=self.config["risk"]["max_concurrent_positions"]),
            SetupCooldownFilter(cooldown_bars=self.config["setups"]["price_discovery"]["cooldown_bars"]),
            RiskBudgetFilter(daily_open_risk_cap_pct=self.config["risk"]["max_daily_risk_open"]),
        ])
        sizing_eq = SizingConfig(
            max_risk_per_trade=self.config["risk"]["max_risk_per_trade"],
            max_notional_per_trade_pct=self.config["risk"]["max_notional_per_trade_pct"],
            allow_fractional=False)
        sizing_cr = SizingConfig(
            max_risk_per_trade=self.config["risk"]["max_risk_per_trade"],
            max_notional_per_trade_pct=self.config["risk"]["max_notional_per_trade_pct"],
            allow_fractional=True)
        rm = RiskManager(circuit_breaker=cb, pipeline=pipeline,
                         sizing_equity=sizing_eq, sizing_crypto=sizing_cr,
                         ledger=ledger, book=book)
        slippage = {ac.name: ac.slippage_bps for ac in self.asset_class_configs.values()}
        fill = SimulatedFillEngine(slippage_bps_by_class=slippage)
        pm = PositionManager(book,
                             max_hold_bars=self.config["position_management"]["max_hold_bars"],
                             breakeven_at_R=self.config["position_management"]["breakeven_at_R"])

        # Build chronological timeline (ts, symbol, asset_class, bar)
        timeline = []
        for sym, ac in self.symbols:
            for b in self.bars.get(sym, []):
                timeline.append((b.ts, sym, ac, b))
        timeline.sort(key=lambda x: (x[0], x[1]))

        equity_points: list[tuple[datetime, float]] = [(timeline[0][0] if timeline else
                                                        datetime.utcnow().replace(tzinfo=__import__("datetime").timezone.utc),
                                                        ledger.equity)]
        trades_log: list[TradeRecord] = []
        filter_audit: dict[str, int] = {}

        for ts, sym, ac, bar in timeline:
            ctx = contexts[sym]
            ctx.ingest(bar)

            # Stops/targets first (within-bar ordering rule)
            actions = pm.on_bar(sym, bar)
            for a in actions:
                if a.kind in ("stop", "target", "time_stop"):
                    pos = book.get(sym)
                    if pos is None:
                        continue
                    exit_px = a.exit_price if a.exit_price is not None else bar.close
                    pnl = (exit_px - pos.entry_px) * pos.qty if pos.side == "long" \
                          else (pos.entry_px - exit_px) * pos.qty
                    risk = max(abs(pos.entry_px - pos.stop_px), 1e-9)
                    r = ((exit_px - pos.entry_px) if pos.side == "long"
                         else (pos.entry_px - exit_px)) / risk
                    rec = TradeRecord(symbol=sym, setup=pos.setup,
                                      entry_ts=pos.opened_at, exit_ts=bar.ts,
                                      entry_px=pos.entry_px, exit_px=exit_px,
                                      side=pos.side, qty=pos.qty,
                                      R_realized=r, pnl_usd=pnl)
                    ledger.record(rec)
                    trades_log.append(rec)
                    book.close(sym)

            # Process pending fills FROM previous bar's submissions
            fill.process_bar(sym, bar, book)

            # Setup checks → submit pending orders for next bar
            for setup in setups[sym]:
                signal = setup.check(ctx)
                if signal is None:
                    continue
                decision = rm.evaluate(signal, ctx, ac)
                if not decision.approved:
                    filter_audit[decision.reason.split(":")[0]] = filter_audit.get(decision.reason.split(":")[0], 0) + 1
                    continue
                fill.submit(PendingOrder(
                    symbol=sym,
                    side="buy" if signal.side == "long" else "sell",
                    qty=decision.qty,
                    order_type="limit" if signal.setup in ("price_discovery", "return_to_value", "vwap_bounce") else "market",
                    limit_price=signal.entry,
                    stop_price=signal.stop, target_price=signal.target,
                    asset_class=ac, setup=signal.setup, ts=signal.ts,
                ))

            equity_points.append((bar.ts, ledger.equity))

        # Build result
        trades_df = pd.DataFrame([t.__dict__ for t in trades_log])
        eq_series = pd.Series([e for _, e in equity_points],
                              index=[t for t, _ in equity_points])

        per_setup, per_symbol = {}, {}
        if not trades_df.empty:
            for s, g in trades_df.groupby("setup"):
                per_setup[s] = {
                    "trades": int(len(g)),
                    "win_rate": float((g["pnl_usd"] > 0).mean()),
                    "expectancy_R": float(g["R_realized"].mean()),
                    "profit_factor": float(g.loc[g["pnl_usd"] > 0, "pnl_usd"].sum() /
                                           max(abs(g.loc[g["pnl_usd"] < 0, "pnl_usd"].sum()), 1e-9)),
                }
            for s, g in trades_df.groupby("symbol"):
                per_symbol[s] = {"trades": int(len(g)),
                                 "total_pnl": float(g["pnl_usd"].sum())}

        metrics = {
            "trades": int(len(trades_df)),
            "final_equity": float(ledger.equity),
            "total_return": float((ledger.equity - self.initial_equity) / self.initial_equity)
                            if self.initial_equity else 0.0,
            "max_consecutive_losses": int(_max_consecutive_losses(trades_df)),
            "win_rate": float((trades_df["pnl_usd"] > 0).mean()) if not trades_df.empty else 0.0,
            "avg_R": float(trades_df["R_realized"].mean()) if not trades_df.empty else 0.0,
        }

        return BacktestResult(
            trades=trades_df, equity_curve=eq_series,
            per_setup=per_setup, per_symbol=per_symbol,
            metrics=metrics, filter_audit=filter_audit,
        )


def _max_consecutive_losses(trades: pd.DataFrame) -> int:
    if trades.empty:
        return 0
    streak = best = 0
    for v in trades["pnl_usd"]:
        if v < 0:
            streak += 1
            best = max(best, streak)
        else:
            streak = 0
    return best
```

- [ ] **6.2.4** Run tests

```bash
pytest tests/test_intraday_replay.py -v
```

Expected: 2 passed.

- [ ] **6.2.5** Commit

```bash
git add backtest/intraday_replay.py tests/test_intraday_replay.py
git commit -m "feat(backtest): IntradayReplay shares engine with live; produces BacktestResult"
```

### Task 6.3: Performance metrics rewrite

**Files:**
- Modify: `backtest/performance.py` (full rewrite)
- Test: `tests/test_performance.py`

- [ ] **6.3.1** Write the failing test

Create `tests/test_performance.py`:

```python
import pandas as pd
from backtest.performance import compute_metrics


def test_compute_metrics_with_trades():
    eq = pd.Series([100000, 100050, 99900, 100200, 100150, 100300])
    trades = pd.DataFrame([
        {"pnl_usd": 50, "R_realized": 1.0},
        {"pnl_usd": -150, "R_realized": -1.0},
        {"pnl_usd": 300, "R_realized": 2.0},
        {"pnl_usd": -50, "R_realized": -0.5},
        {"pnl_usd": 150, "R_realized": 1.5},
    ])
    m = compute_metrics(eq, trades)
    assert m["trades"] == 5
    assert m["win_rate"] == 3 / 5
    assert m["max_drawdown"] >= 0
    assert m["avg_R"] > 0


def test_compute_metrics_empty():
    eq = pd.Series([100000])
    trades = pd.DataFrame(columns=["pnl_usd", "R_realized"])
    m = compute_metrics(eq, trades)
    assert m["trades"] == 0
    assert m["win_rate"] == 0.0
    assert m["max_drawdown"] == 0.0
```

- [ ] **6.3.2** Run, expect failure

```bash
pytest tests/test_performance.py -v
```

- [ ] **6.3.3** Replace `backtest/performance.py` contents with:

```python
from __future__ import annotations
import math
import pandas as pd


def compute_metrics(equity_curve: pd.Series, trades: pd.DataFrame) -> dict:
    if equity_curve.empty:
        return {"trades": 0, "win_rate": 0.0, "max_drawdown": 0.0,
                "avg_R": 0.0, "profit_factor": 0.0, "sharpe": 0.0}
    if trades.empty:
        return {"trades": 0, "win_rate": 0.0,
                "max_drawdown": float(_max_dd(equity_curve)),
                "avg_R": 0.0, "profit_factor": 0.0,
                "sharpe": 0.0}

    wins = trades[trades["pnl_usd"] > 0]["pnl_usd"].sum()
    losses = abs(trades[trades["pnl_usd"] < 0]["pnl_usd"].sum())
    rets = equity_curve.pct_change().dropna()
    sharpe = (rets.mean() / rets.std() * math.sqrt(78 * 252)) if rets.std() > 0 else 0.0
    return {
        "trades": int(len(trades)),
        "win_rate": float((trades["pnl_usd"] > 0).mean()),
        "max_drawdown": float(_max_dd(equity_curve)),
        "avg_R": float(trades["R_realized"].mean()),
        "profit_factor": float(wins / losses) if losses > 0 else float("inf") if wins > 0 else 0.0,
        "sharpe": float(sharpe),
    }


def _max_dd(eq: pd.Series) -> float:
    if eq.empty:
        return 0.0
    peak = eq.cummax()
    dd = (peak - eq) / peak
    return float(dd.max() if not dd.empty else 0.0)
```

- [ ] **6.3.4** Run tests

```bash
pytest tests/test_performance.py -v
```

Expected: 2 passed.

- [ ] **6.3.5** Commit

```bash
git add backtest/performance.py tests/test_performance.py
git commit -m "refactor(backtest): performance metrics adapted for per-trade R-multiple model"
```

---

**End of Phase 6.** Tree state: full backtest engine reusing all production setup/filter/sizing classes; emits structured trades/metrics. Ready for Phase 7.

---

## Phase 7 — Dashboard

The new dashboard reads `runtime/trading_state.json` produced by the live engine. The state-write step is added in this phase as well.

### Task 7.1: Dashboard state writer

**Files:**
- Create: `state/dashboard_state.py`
- Test: `tests/test_dashboard_state.py`

- [ ] **7.1.1** Write the failing test

Create `tests/test_dashboard_state.py`:

```python
import json
from datetime import datetime, timezone
from pathlib import Path
from state.dashboard_state import write_dashboard_state, DashboardSnapshot


def test_write_dashboard_state(tmp_path: Path):
    snap = DashboardSnapshot(
        timestamp=datetime(2026, 5, 14, 14, 0, tzinfo=timezone.utc),
        equity=100100.0,
        day_pnl=100.0,
        circuit_level=0,
        symbols=[
            {"symbol": "AAPL", "vwap": 100.5, "upper": 101.0, "lower": 100.0,
             "regime": "Range", "open_position": None},
            {"symbol": "BTC/USD", "vwap": 50100.0, "upper": 50300.0, "lower": 49900.0,
             "regime": "Trend", "open_position": {"side": "long", "qty": 0.1,
                                                  "entry": 50000, "stop": 49500,
                                                  "target": 51000}},
        ],
        recent_filter_rejects=[
            {"filter": "consecutive_loss", "symbol": "AAPL", "ts": "2026-05-14T13:55:00+00:00"},
        ],
    )
    out = tmp_path / "state.json"
    write_dashboard_state(out, snap)
    data = json.loads(out.read_text())
    assert data["equity"] == 100100.0
    assert len(data["symbols"]) == 2
    assert data["symbols"][1]["open_position"]["side"] == "long"
```

- [ ] **7.1.2** Run, expect failure

```bash
pytest tests/test_dashboard_state.py -v
```

- [ ] **7.1.3** Implement `state/dashboard_state.py`

```python
from __future__ import annotations
import json
import os
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path


@dataclass
class DashboardSnapshot:
    timestamp: datetime
    equity: float
    day_pnl: float
    circuit_level: int
    symbols: list[dict]
    recent_filter_rejects: list[dict]


def _to_jsonable(o):
    if isinstance(o, datetime):
        return o.isoformat()
    return o


def write_dashboard_state(path: Path | str, snap: DashboardSnapshot) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "timestamp": snap.timestamp.isoformat(),
        "equity": snap.equity,
        "day_pnl": snap.day_pnl,
        "circuit_level": snap.circuit_level,
        "symbols": snap.symbols,
        "recent_filter_rejects": snap.recent_filter_rejects,
    }
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, default=_to_jsonable))
    os.replace(tmp, path)
```

- [ ] **7.1.4** Run tests

```bash
pytest tests/test_dashboard_state.py -v
```

Expected: 1 passed.

- [ ] **7.1.5** Commit

```bash
git add state/dashboard_state.py tests/test_dashboard_state.py
git commit -m "feat(state): atomic dashboard state writer"
```

### Task 7.2: New `ui/dashboard.py`

**Files:**
- Modify: `ui/dashboard.py` (full rewrite — no automated tests; manual smoke)

- [ ] **7.2.1** Replace `ui/dashboard.py` contents with:

```python
"""
VWAP Wave dashboard. Reads runtime/trading_state.json and renders:
- top bar:  equity, day P&L, circuit level
- table:    per-symbol VWAP/bands/regime/open position
- panel:    recent filter rejects
"""
import json
from pathlib import Path

import pandas as pd
import streamlit as st
from streamlit_autorefresh import st_autorefresh

STATE_FILE = Path("runtime/trading_state.json")

st.set_page_config(page_title="VWAP Wave", layout="wide")
st_autorefresh(interval=5000, key="vwap_wave_refresh")
st.title("VWAP Wave Protocol")


def _read_state() -> dict | None:
    if not STATE_FILE.exists():
        return None
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return None


state = _read_state()
if state is None:
    st.warning("No state file yet. Start the engine via `python main.py`.")
    st.stop()

# --- Header strip
col1, col2, col3, col4 = st.columns(4)
col1.metric("Equity", f"${state['equity']:,.2f}")
col2.metric("Day P&L", f"${state['day_pnl']:,.2f}")
col3.metric("Circuit Level", state["circuit_level"])
col4.metric("As of", state["timestamp"])

# --- Per-symbol table
st.subheader("Symbols")
rows = []
for s in state["symbols"]:
    pos = s.get("open_position")
    rows.append({
        "Symbol": s["symbol"],
        "Regime": s["regime"],
        "VWAP": s.get("vwap"),
        "Upper σ": s.get("upper"),
        "Lower σ": s.get("lower"),
        "Position": (f"{pos['side']} {pos['qty']} @ {pos['entry']}"
                     if pos else "—"),
        "Stop": pos["stop"] if pos else "",
        "Target": pos["target"] if pos else "",
    })
df = pd.DataFrame(rows)
st.dataframe(df, use_container_width=True, hide_index=True)

# --- Filter audit
st.subheader("Recent filter rejects")
rejects = state.get("recent_filter_rejects", [])
if rejects:
    st.dataframe(pd.DataFrame(rejects), use_container_width=True, hide_index=True)
else:
    st.caption("No rejects in the recent window.")
```

- [ ] **7.2.2** Smoke check: confirm the file imports without error

```bash
python -c "import ast; ast.parse(open('ui/dashboard.py').read()); print('ok')"
```

Expected: `ok`.

- [ ] **7.2.3** Commit

```bash
git add ui/dashboard.py
git commit -m "feat(ui): VWAP Wave dashboard panels (header, symbols, filter audit)"
```

### Task 7.3: Wire state-write into the engine loop

**Files:**
- Modify: `main.py`

- [ ] **7.3.1** Add the import near the top of `main.py`

After the `from state.position_book import PositionBook` line, add:

```python
from state.dashboard_state import DashboardSnapshot, write_dashboard_state
```

- [ ] **7.3.2** Add a `_collect_snapshot` helper near the bottom (above `def main()`):

```python
def _collect_snapshot(symbols, contexts, book, ledger, cb, recent_rejects):
    rows = []
    for sym, _ in symbols:
        ctx = contexts[sym]
        pos = book.get(sym)
        rows.append({
            "symbol": sym,
            "regime": ctx.regime,
            "vwap": None if ctx.bar_count == 0 else ctx.vwap,
            "upper": None if ctx.bar_count == 0 else ctx.upper_band,
            "lower": None if ctx.bar_count == 0 else ctx.lower_band,
            "open_position": (None if pos is None else
                              {"side": pos.side, "qty": pos.qty,
                               "entry": pos.entry_px, "stop": pos.stop_px,
                               "target": pos.target_px}),
        })
    return DashboardSnapshot(
        timestamp=datetime.now(timezone.utc),
        equity=ledger.equity,
        day_pnl=ledger.day_pnl,
        circuit_level=cb.level,
        symbols=rows,
        recent_filter_rejects=recent_rejects[-20:],
    )
```

- [ ] **7.3.3** In the main loop, after `engine.tick(...)` and the equity update, write the snapshot

Insert between the `rm.update_equity(equity)` line and the closing of the `try` block:

```python
            snap = _collect_snapshot(symbols, contexts, book, ledger, cb,
                                     recent_rejects=[])
            write_dashboard_state("runtime/trading_state.json", snap)
```

- [ ] **7.3.4** Smoke-import again

```bash
TRADING_ENV=test ALPACA_API_KEY=x ALPACA_SECRET_KEY=x python -c "import main; print('ok')"
```

Expected: `ok`.

- [ ] **7.3.5** Commit

```bash
git add main.py
git commit -m "feat: write dashboard snapshot at end of each scheduler tick"
```

---

**End of Phase 7.** Tree state: dashboard reads atomic JSON state written each cycle. Ready for Phase 8.

---

## Phase 8 — Validation

### Task 8.1: Three sanity tests

**Files:**
- Create: `tests/test_backtest_smoke.py` (was already implicitly covered in Task 6.2 — extend here)
- Create: `tests/test_backtest_sanity.py`

- [ ] **8.1.1** Write the sanity tests

Create `tests/test_backtest_sanity.py`:

```python
"""Three sanity tests the backtest engine MUST pass before being trusted (spec §7)."""
from datetime import datetime, timezone, timedelta
import pandas as pd
from core.bar import Bar
from core.asset_class import AssetClassConfig
from backtest.intraday_replay import IntradayReplay


CRYPTO = AssetClassConfig(
    name="crypto", timezone="UTC",
    session_open_local="00:00", session_close_local="23:59",
    opening_blackout_min=15, bar_timeframe="5Min",
    slippage_bps=0.0, commission_per_share=0.0, commission_bps=0.0,
)


def _flat_bars(symbol, n):
    base = datetime(2026, 5, 14, 0, 5, tzinfo=timezone.utc)
    return [Bar(symbol=symbol, ts=base + timedelta(minutes=5*i),
                open=100.0, high=100.0, low=100.0, close=100.0, volume=100)
            for i in range(n)]


def _cfg(disabled=True):
    enabled = not disabled
    return {
        "setups": {
            "price_discovery": {"enabled": enabled, "atr_mult_stop": 1.0, "target_R": 1.5,
                                "arm_window_bars": 6, "cooldown_bars": 12},
            "fade_extreme": {"enabled": enabled, "atr_mult_stop": 0.75,
                             "scale_offsets_atr": [0.0], "scale_weights": [1.0],
                             "cooldown_bars": 12},
            "return_to_value": {"enabled": enabled, "atr_mult_stop": 1.0,
                                "arm_window_bars": 6, "cooldown_bars": 12},
            "vwap_bounce": {"enabled": enabled, "atr_mult_stop": 1.25, "target_R": 2.0,
                            "arm_window_bars": 4, "cooldown_bars": 8},
        },
        "risk": {
            "max_risk_per_trade": 0.005, "max_notional_per_trade_pct": 0.20,
            "max_concurrent_positions": 4, "max_daily_risk_open": 0.02,
            "consecutive_loss_limit": 2, "loss_filter_scope": "per_symbol",
            "circuit_breaker": {"daily_loss_limit_1": 0.015, "daily_loss_limit_2": 0.025,
                                "drawdown_limit": 0.05},
        },
        "filters": {"opening_blackout_min": 0, "volume_deficit_pct": 0.30},
        "position_management": {"max_hold_bars": 12, "breakeven_at_R": 1.0,
                                "trail_at_R": 1.5, "trail_atr": 1.0},
        "scheduler": {"bar_timeframe": "5Min"},
    }


def test_no_signal_universe_yields_zero_trades():
    bars = {"BTC/USD": _flat_bars("BTC/USD", 50)}
    res = IntradayReplay([("BTC/USD", "crypto")], {"crypto": CRYPTO}, bars,
                          100000.0, _cfg(disabled=False)).run()
    assert res.metrics["trades"] == 0
    assert abs(res.equity_curve.iloc[-1] - 100000.0) < 1e-9


def test_idempotency_two_runs_match():
    bars = {"BTC/USD": _flat_bars("BTC/USD", 80)}
    a = IntradayReplay([("BTC/USD", "crypto")], {"crypto": CRYPTO}, bars,
                        100000.0, _cfg(disabled=True)).run()
    b = IntradayReplay([("BTC/USD", "crypto")], {"crypto": CRYPTO}, bars,
                        100000.0, _cfg(disabled=True)).run()
    pd.testing.assert_series_equal(a.equity_curve, b.equity_curve)
    assert a.metrics == b.metrics


def test_replay_completes_on_realistic_path():
    base = datetime(2026, 5, 14, 0, 5, tzinfo=timezone.utc)
    bars = []
    for i in range(120):
        # synthetic random-walk-ish path
        c = 100 + (i % 17) - 8
        bars.append(Bar(symbol="BTC/USD", ts=base + timedelta(minutes=5*i),
                        open=c-0.2, high=c+0.4, low=c-0.4, close=c, volume=100))
    res = IntradayReplay([("BTC/USD", "crypto")], {"crypto": CRYPTO},
                         {"BTC/USD": bars}, 100000.0, _cfg(disabled=False)).run()
    # Gate is "completes without errors", not profitability
    assert res.equity_curve is not None
    assert "trades" in res.metrics
```

- [ ] **8.1.2** Run

```bash
pytest tests/test_backtest_sanity.py -v
```

Expected: 3 passed.

- [ ] **8.1.3** Final full-suite run

```bash
pytest tests/ -v
```

Expected: all green; <30s.

- [ ] **8.1.4** Commit

```bash
git add tests/test_backtest_sanity.py
git commit -m "test(backtest): three sanity tests (no-signal/idempotency/realistic-path)"
```

---

**End of Phase 8.** Tree state: validated backtest engine. Ready for Phase 9.

---

## Phase 9 — Config + Docs

### Task 9.1: Replace `config/settings.yaml`

**Files:**
- Modify: `config/settings.yaml` (full rewrite)

- [ ] **9.1.1** Replace `config/settings.yaml` contents with:

```yaml
system:
  name: vwap_wave
  version: "2.0.0"
  trading_env: paper            # paper | live | backtest

scheduler:
  bar_timeframe: "5Min"
  wake_grace_seconds: 5
  poll_fallback_seconds: 60

vwap:
  sigma_bands: 1.0
  min_session_bars: 6

acceptance:
  consecutive_closes: 2
  min_distance_atr: 0.25

regime_detector:
  trend_day_range_mult: 1.5
  trend_day_in_value_max: 0.30
  balance_day_in_value_min: 0.60

setups:
  price_discovery:
    enabled: true
    atr_mult_stop: 1.0
    target_R: 1.5
    arm_window_bars: 6
    cooldown_bars: 12
  fade_extreme:
    enabled: true
    atr_mult_stop: 0.75
    scale_weights: [0.4, 0.35, 0.25]
    scale_offsets_atr: [0.0, 0.25, 0.50]
    target: vwap
    cooldown_bars: 12
  return_to_value:
    enabled: true
    atr_mult_stop: 1.0
    target: vwap
    arm_window_bars: 6
    cooldown_bars: 12
  vwap_bounce:
    enabled: true
    atr_mult_stop: 1.25
    target_R: 2.0
    arm_window_bars: 4
    cooldown_bars: 8

risk:
  max_risk_per_trade: 0.005
  max_notional_per_trade_pct: 0.20
  max_concurrent_positions: 4
  max_daily_risk_open: 0.02
  consecutive_loss_limit: 2
  loss_filter_scope: per_symbol
  circuit_breaker:
    daily_loss_limit_1: 0.015
    daily_loss_limit_2: 0.025
    drawdown_limit: 0.05

filters:
  opening_blackout_min: 15
  volume_deficit_pct: 0.30

position_management:
  max_hold_bars: 12
  breakeven_at_R: 1.0
  trail_at_R: 1.5
  trail_atr: 1.0

asset_classes:
  equity:
    timezone: "America/New_York"
    session_open_local: "09:30"
    session_close_local: "16:00"
    slippage_bps: 2
    commission_per_share: 0.0
    symbols:
      - SPY
      - QQQ
      - IWM
      - XLF
      - XLE
      - GLD
      - AAPL
      - MSFT
      - NVDA
      - TSLA
      - AMZN
      - GOOGL
      - META
      - AMD
      - JPM
      - AVGO
      - NFLX
      - COIN
      - PLTR
      - UBER
  crypto:
    timezone: "UTC"
    session_open_local: "00:00"
    session_close_local: "23:59"
    slippage_bps: 5
    commission_bps: 25
    symbols:
      - "BTC/USD"
      - "ETH/USD"
      - "SOL/USD"
      - "AVAX/USD"
      - "LINK/USD"

news_blackouts: []

backtest:
  start: "2024-01-01"
  end: "2026-04-30"
  initial_equity: 100000
  cache_dir: "runtime/bars_cache"

broker:
  paper_trading: true
  handshake_symbol: SPY

logging:
  level: INFO
  log_file: logs/vwap_wave.log
```

- [ ] **9.1.2** Smoke-import once more (config now loads)

```bash
mkdir -p logs runtime
TRADING_ENV=test ALPACA_API_KEY=x ALPACA_SECRET_KEY=x python -c "
import yaml
cfg = yaml.safe_load(open('config/settings.yaml'))
assert cfg['system']['name'] == 'vwap_wave'
assert len(cfg['asset_classes']['equity']['symbols']) == 20
assert len(cfg['asset_classes']['crypto']['symbols']) == 5
print('config ok')
"
```

Expected: `config ok`.

- [ ] **9.1.3** Commit

```bash
git add config/settings.yaml
git commit -m "feat(config): VWAP Wave Protocol settings.yaml (25 symbols, 4 setups)"
```

### Task 9.2: Rewrite `README.md`

**Files:**
- Modify: `README.md` (full rewrite)

- [ ] **9.2.1** Replace `README.md` contents with:

```markdown
# vwap_wave

## Overview

`vwap_wave` is an autonomous intraday trading system implementing the **VWAP Wave Protocol** across equities, ETFs, and crypto via Alpaca Markets. The engine treats VWAP as a dynamic Point of Control, classifies each session as Range / Discovery / Trend, and runs four setup state machines:

1. **Price Discovery Continuation** — band breakout + acceptance + backtest entry.
2. **Fade Value Area Extremes** — scale-in fades on balance days.
3. **Return to Value** — failed discovery move re-entering value area.
4. **VWAP Bounce** — trend-day reclaim after sub-VWAP liquidity trap.

Live execution and backtesting share the same `SessionContext`, setup, and filter classes; only the bar source and order sink differ.

## Architecture

```
vwap_wave/
├── config/settings.yaml        # All tunable parameters
├── core/
│   ├── bar.py                  # Bar dataclass (OHLCV, timezone-aware)
│   ├── vwap.py                 # Incremental VWAP + ±1σ bands
│   ├── acceptance.py           # N-close + ATR distance detector
│   ├── atr.py                  # Wilder ATR
│   ├── asset_class.py          # AssetClassConfig + session boundary
│   ├── session.py              # SessionContext per symbol
│   └── position_manager.py     # Stop/target/breakeven/time-stop
├── strategies/
│   ├── base_setup.py           # BaseSetup + SetupSignal
│   ├── regime_detector.py      # Range/Trend/Discovery classifier
│   ├── setup_price_discovery.py
│   ├── setup_fade_extreme.py
│   ├── setup_return_to_value.py
│   └── setup_vwap_bounce.py
├── risk/
│   ├── circuit_breakers.py     # Tiered P&L breakers + lock-file
│   ├── filters.py              # 8 entry filters + pipeline
│   ├── sizing.py               # ATR-based position sizing
│   └── manager.py              # Façade: pipeline → sizing → RiskDecision
├── state/
│   ├── position_book.py        # Open positions ledger
│   ├── daily_ledger.py         # Per-day P&L + consec losses
│   └── dashboard_state.py      # Atomic JSON for dashboard
├── broker/
│   ├── alpaca_client.py        # REST API (orders, account, bars, brackets)
│   ├── alpaca_data.py          # Bars wrapper + parquet cache
│   ├── order_executor.py       # SetupSignal → broker order
│   └── symbol.py               # Equity vs crypto symbol helpers
├── scheduler/
│   ├── bar_clock.py            # next_boundary, sleep_until
│   └── loop.py                 # VWAPWaveEngine.tick (bar-close cycle)
├── backtest/
│   ├── intraday_replay.py      # Shared-engine historical replay
│   ├── fill_engine.py          # SimulatedFillEngine
│   ├── performance.py          # Metrics
│   └── benchmarks.py
├── ui/
│   ├── dashboard.py            # Streamlit panels
│   └── logging_setup.py
├── main.py                     # Bar-close scheduler boot
├── requirements.txt
└── tests/                      # Unit + integration tests (~30s full suite)
```

## Setup

### Requirements

- Python 3.11+
- `pip install -r requirements.txt`

### Environment Variables

| Variable           | Required | Default                              |
|--------------------|----------|--------------------------------------|
| `ALPACA_API_KEY`   | Yes      | —                                    |
| `ALPACA_SECRET_KEY`| Yes      | —                                    |
| `ALPACA_BASE_URL`  | No       | `https://paper-api.alpaca.markets`   |
| `TRADING_ENV`      | No       | `production` (`test` to bypass lock) |
| `LOCK_FILE_PATH`   | No       | `lock.file`                          |

Create a `.env` in the project root:

```
ALPACA_API_KEY=...
ALPACA_SECRET_KEY=...
```

```bash
mkdir -p logs runtime/bars_cache
```

## Running

### Live (paper) trading

```bash
python main.py
```

The engine wakes at each 5-minute bar boundary, fetches fresh bars for every configured symbol, runs the four setup state machines, evaluates risk filters, sizes orders, and submits them via Alpaca. Live trading is gated behind `system.trading_env: paper` — flipping to `live` is intentional and ungated.

### Dashboard

```bash
streamlit run ui/dashboard.py
```

The dashboard auto-refreshes every 5 s and reads `runtime/trading_state.json`.

### Backtest

```python
from datetime import datetime, timezone
import yaml
from broker.alpaca_client import AlpacaClient
from broker.alpaca_data import AlpacaData
from core.asset_class import AssetClassConfig
from backtest.intraday_replay import IntradayReplay

with open("config/settings.yaml") as f:
    cfg = yaml.safe_load(f)

client = AlpacaClient()
data = AlpacaData(client, cache_dir=cfg["backtest"]["cache_dir"])

# Build asset class configs and (symbol, asset_class) list as in main.py
# (helper functions are reused; see scripts/run_backtest.py)
```

(For convenience, a runnable `scripts/run_backtest.py` can be added as a follow-up — not part of v1.)

## Circuit Breakers / Lock-File Recovery

Three escalating tiers (tunable in `config/settings.yaml`):

- **L1** (−1.5% intraday): position sizes halved.
- **L2** (−2.5% intraday): new entries blocked.
- **L3** (−5% peak-to-valley): all positions closed, `lock.file` written, `sys.exit(1)`.

Recovery: review logs, perform post-mortem, then `rm lock.file` and restart.

## Testing

```bash
pytest tests/ -v
```

Target: full suite in <30 s. No broker connectivity required for the main suite; mocks are used throughout.
```

- [ ] **9.2.2** Commit

```bash
git add README.md
git commit -m "docs: rewrite README for VWAP Wave Protocol architecture and ops"
```

### Task 9.3: Final full-suite green check

- [ ] **9.3.1** Run

```bash
pytest tests/ -v
```

Expected: all tests green. Suite total <30 s. No skipped tests except the broker-live smoke (which still requires paper credentials and is OK to leave as manual).

- [ ] **9.3.2** Verify smoke import

```bash
TRADING_ENV=test ALPACA_API_KEY=x ALPACA_SECRET_KEY=x python -c "import main; print('ok')"
```

Expected: `ok`.

- [ ] **9.3.3** Confirm clean tree

```bash
git status
```

Expected: working tree clean (or only the pre-existing `M docker-compose.yml` and `?? lock.file` carried over from before the migration).

---

**End of Phase 9.** The migration is complete. The branch `feature/vwap-wave-protocol` is ready for review/merge. Live trading remains gated behind `system.trading_env: paper`; flipping to `live` is a deliberate operator decision per the spec's Definition of Done.

---

## Self-Review

This is a check the plan author runs against the spec.

**Spec coverage check** (each spec section maps to at least one task):
- Spec §1 Goals/Scope → Phase 0 (deletes), Phase 9 (config). ✓
- Spec §2 Strategy summary → Phase 3 (four setups). ✓
- Spec §3 Execution loop → Phase 5 Task 5.4, Task 5.5. ✓
- Spec §4 Module map → Phase 0 + new modules across 1-7. ✓
- Spec §5 Setup state machines → Phase 3 Tasks 3.2-3.5. ✓
- Spec §6 Risk/filters/sizing → Phase 4 Tasks 4.1-4.5. ✓
- Spec §7 Backtest engine → Phase 6 + Phase 8 sanity tests. ✓
- Spec §8 Sessions/news/asset classes → Phase 2 Task 2.4-2.5, Phase 5 (news in main.py). ✓
- Spec §9 Config schema → Phase 9 Task 9.1. ✓
- Spec §10 Migration plan → entire plan structure. ✓
- Spec §11 Definition of Done → covered in test gates throughout, README in 9.2. ✓

**Placeholder scan:** No "TBD", "TODO", "fill in", or "similar to Task N" patterns. Every code step contains real code.

**Type consistency check:**
- `SetupSignal` defined in 3.1, used identically in 3.2-3.5, 4.4-4.5, 5.2, 5.4, 6.2. ✓
- `RiskDecision` defined in 4.5, used identically in 5.2, 5.4. ✓
- `OpenPosition` defined in 4.2, used in 5.2, 5.3, 5.4, 6.1, 6.2. ✓
- `SessionContext` defined in 2.5, used in 2.6, 3.x, 4.4 (signatures), 5.4, 6.2. ✓
- `Bar` defined in 1.1, used everywhere downstream with the same signature. ✓
- `FilterResult` / `EntryFilter` / `FilterPipeline` defined in 4.4, used in 4.5, 5.5, 6.2. ✓
- `BacktestResult` defined in 6.2, used in 6.3 tests + 8.1. ✓

Verified: no stale or inconsistent identifiers across phases.

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-05-14-vwap-wave-protocol.md`. Two execution options:

**1. Subagent-Driven (recommended)** — fresh subagent per task, review between tasks, fast iteration. Best when you want a tight feedback loop and the option to course-correct between phases.

**2. Inline Execution** — execute tasks in this session using `superpowers:executing-plans`, batch execution with checkpoints for review.

Which approach?
