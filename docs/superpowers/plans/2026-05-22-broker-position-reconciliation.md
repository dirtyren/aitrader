# Broker Position Reconciliation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the in-memory `PositionBook` match Alpaca's reality at startup and on every cycle, by fetching `/v2/positions` and adopting orphans as monitor-only.

**Architecture:** New `state/reconciler.py` module with a `Reconciler` class. Called from `main.py` at startup (fail-start on raise) and once per cycle just before `engine.tick()` (log-and-continue on raise). `OpenPosition` gains an `adopted: bool` flag; `PositionManager.on_bar` skips lifecycle actions on adopted positions. New `AlpacaClient.list_orders` recovers bracket children (`stop_px`, `target_px`, `stop_order_id`) for adopted equity positions.

**Tech Stack:** Python 3.11, pytest, existing `requests`-based `AlpacaClient`, no new dependencies.

**Spec:** `docs/superpowers/specs/2026-05-22-broker-position-reconciliation-design.md`

---

## File Map

| File | Role |
|------|------|
| `state/position_book.py` | Add `adopted` field; relax `stop_px`/`target_px` to `Optional[float]`; guard `risk_per_share` against `None` |
| `core/position_manager.py` | Early-return guard on `pos.adopted` |
| `broker/alpaca_client.py` | New `list_orders` method |
| `state/reconciler.py` | **NEW** — `ReconcileReport`, `Reconciler`, internal helpers |
| `main.py` | Instantiate reconciler; call at startup and per cycle |
| `tests/test_position_book.py` | Tests for None-stop and `adopted` field |
| `tests/test_position_manager.py` | Tests for adopted-skip behavior |
| `tests/test_alpaca_client_list_orders.py` | **NEW** — list_orders unit tests |
| `tests/test_reconciler.py` | **NEW** — full reconciler test suite |

---

## Task 1: Extend `OpenPosition` with `adopted` flag and optional stop/target

**Files:**
- Modify: `state/position_book.py:6-33`
- Test: `tests/test_position_book.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_position_book.py`:

```python
def test_open_position_adopted_defaults_false():
    p = OpenPosition(symbol="AAPL", setup="price_discovery", side="long",
                     qty=10, entry_px=100.0, stop_px=99.0, target_px=102.0,
                     opened_at=datetime(2026, 5, 14, 14, 0, tzinfo=timezone.utc),
                     order_id="abc")
    assert p.adopted is False


def test_open_position_adopted_can_be_set_true():
    p = OpenPosition(symbol="AAPL", setup="adopted", side="long",
                     qty=10, entry_px=100.0, stop_px=None, target_px=None,
                     opened_at=datetime(2026, 5, 14, 14, 0, tzinfo=timezone.utc),
                     order_id="", adopted=True)
    assert p.adopted is True


def test_open_position_with_none_stop_yields_zero_risk():
    p = OpenPosition(symbol="AAPL", setup="adopted", side="long",
                     qty=10, entry_px=100.0, stop_px=None, target_px=None,
                     opened_at=datetime(2026, 5, 14, 14, 0, tzinfo=timezone.utc),
                     order_id="", adopted=True)
    assert p.risk_per_share == 0.0
    assert p.initial_risk_per_share == 0.0
    assert p.open_risk_usd == 0.0


def test_aggregate_open_risk_skips_none_stop_positions():
    book = PositionBook()
    book.add(OpenPosition(symbol="AAPL", setup="x", side="long", qty=10,
                          entry_px=100.0, stop_px=99.0, target_px=102.0,
                          opened_at=datetime.now(timezone.utc), order_id="a"))
    book.add(OpenPosition(symbol="BTC/USD", setup="adopted", side="long", qty=1,
                          entry_px=50_000.0, stop_px=None, target_px=None,
                          opened_at=datetime.now(timezone.utc), order_id="",
                          adopted=True))
    assert book.aggregate_open_risk_usd() == 10.0  # only the AAPL position contributes
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_position_book.py::test_open_position_adopted_defaults_false \
       tests/test_position_book.py::test_open_position_adopted_can_be_set_true \
       tests/test_position_book.py::test_open_position_with_none_stop_yields_zero_risk \
       tests/test_position_book.py::test_aggregate_open_risk_skips_none_stop_positions -v
```

Expected: FAIL — `OpenPosition` has no `adopted` field; passing `stop_px=None` will fail dataclass type validation only at runtime when `risk_per_share` does the subtraction.

- [ ] **Step 3: Update `OpenPosition` dataclass**

Replace `state/position_book.py` lines 6-33 with:

```python
@dataclass
class OpenPosition:
    symbol: str
    setup: str
    side: str               # "long" | "short"
    qty: float
    entry_px: float
    stop_px: float | None
    target_px: float | None
    opened_at: datetime
    order_id: str
    breakeven_moved: bool = False
    bars_held: int = 0
    stop_order_id: str | None = None    # bracket stop-leg id (equity); None for crypto / virtual / adopted-no-bracket
    initial_stop_px: float | None = None  # original stop at entry; survives breakeven moves for R calc
    adopted: bool = False               # True for positions reconciled from broker (monitor-only)

    @property
    def initial_risk_per_share(self) -> float:
        ref = self.initial_stop_px if self.initial_stop_px is not None else self.stop_px
        if ref is None:
            return 0.0
        return abs(self.entry_px - ref)

    @property
    def risk_per_share(self) -> float:
        if self.stop_px is None:
            return 0.0
        return abs(self.entry_px - self.stop_px)

    @property
    def open_risk_usd(self) -> float:
        return self.risk_per_share * self.qty
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_position_book.py -v
```

Expected: ALL PASS, including the 4 new tests above.

- [ ] **Step 5: Run full test suite to confirm no regressions**

```bash
pytest -v
```

Expected: full suite passes (the schema change is backward-compatible — `adopted` defaults False, `stop_px`/`target_px` are typed as optional but every existing call site still passes a real float).

- [ ] **Step 6: Commit**

```bash
git add state/position_book.py tests/test_position_book.py
git commit -m "feat(state): add adopted flag and None-stop support to OpenPosition

Allows the upcoming reconciler to record positions whose stop/target
metadata isn't recoverable. Risk-per-share returns 0 in that case so
aggregate_open_risk_usd remains correct."
```

---

## Task 2: PositionManager skips lifecycle actions on adopted positions

**Files:**
- Modify: `core/position_manager.py:23-66`
- Test: `tests/test_position_manager.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_position_manager.py`:

```python
def _adopted_pos(side="long", entry=100.0, stop=99.0, target=102.0):
    return OpenPosition(symbol="AAPL", setup="adopted", side=side,
                        qty=10, entry_px=entry, stop_px=stop, target_px=target,
                        opened_at=datetime(2026, 5, 14, 14, 0, tzinfo=timezone.utc),
                        order_id="", adopted=True)


def test_adopted_position_skips_stop_action():
    book = PositionBook()
    book.add(_adopted_pos())  # long, stop 99, entry 100
    pm = PositionManager(book, max_hold_bars=12, breakeven_at_R=1.0)
    actions = pm.on_bar("AAPL",
        _bar(datetime(2026, 5, 14, 14, 5, tzinfo=timezone.utc), 98.5, l=98.0))
    assert actions == []
    # Position remains in book — only the reconciler closes adopted positions
    assert book.get("AAPL") is not None


def test_adopted_position_skips_target_action():
    book = PositionBook()
    book.add(_adopted_pos())
    pm = PositionManager(book, max_hold_bars=12, breakeven_at_R=1.0)
    actions = pm.on_bar("AAPL",
        _bar(datetime(2026, 5, 14, 14, 5, tzinfo=timezone.utc), 102.5, h=103.0))
    assert actions == []
    assert book.get("AAPL") is not None


def test_adopted_position_skips_breakeven():
    book = PositionBook()
    book.add(_adopted_pos())  # long, risk 1, would BE at 101
    pm = PositionManager(book, max_hold_bars=12, breakeven_at_R=1.0)
    pm.on_bar("AAPL",
        _bar(datetime(2026, 5, 14, 14, 5, tzinfo=timezone.utc), 101.2, h=101.5))
    pos = book.get("AAPL")
    assert pos.stop_px == 99.0
    assert pos.breakeven_moved is False


def test_adopted_position_skips_time_stop():
    book = PositionBook()
    book.add(_adopted_pos())
    pm = PositionManager(book, max_hold_bars=2, breakeven_at_R=1.0)
    base = datetime(2026, 5, 14, 14, 0, tzinfo=timezone.utc)
    pm.on_bar("AAPL", _bar(base + timedelta(minutes=5), 100.5))
    pm.on_bar("AAPL", _bar(base + timedelta(minutes=10), 100.5))
    actions = pm.on_bar("AAPL", _bar(base + timedelta(minutes=15), 100.5))
    assert actions == []
    assert book.get("AAPL") is not None


def test_adopted_position_increments_bars_held():
    book = PositionBook()
    book.add(_adopted_pos())
    pm = PositionManager(book, max_hold_bars=12, breakeven_at_R=1.0)
    base = datetime(2026, 5, 14, 14, 0, tzinfo=timezone.utc)
    pm.on_bar("AAPL", _bar(base + timedelta(minutes=5), 100.5))
    pm.on_bar("AAPL", _bar(base + timedelta(minutes=10), 100.5))
    assert book.get("AAPL").bars_held == 2


def test_adopted_position_with_none_stop_does_not_raise():
    book = PositionBook()
    p = OpenPosition(symbol="BTC/USD", setup="adopted", side="long",
                     qty=1, entry_px=50_000.0, stop_px=None, target_px=None,
                     opened_at=datetime(2026, 5, 14, 14, 0, tzinfo=timezone.utc),
                     order_id="", adopted=True)
    book.add(p)
    pm = PositionManager(book, max_hold_bars=12, breakeven_at_R=1.0)
    actions = pm.on_bar("BTC/USD",
        Bar(symbol="BTC/USD",
            ts=datetime(2026, 5, 14, 14, 5, tzinfo=timezone.utc),
            open=49_000.0, high=49_500.0, low=48_000.0, close=48_500.0,
            volume=10))
    assert actions == []
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_position_manager.py -k "adopted" -v
```

Expected: FAIL — current `on_bar` will trigger stop/target/breakeven/time_stop branches; the None-stop test will raise `TypeError: '<=' not supported between 'float' and 'NoneType'`.

- [ ] **Step 3: Add adopted-skip guard to `on_bar`**

Replace `core/position_manager.py:23-26` (the start of `on_bar`) with:

```python
    def on_bar(self, symbol: str, bar: Bar) -> list[PositionAction]:
        pos = self._book.get(symbol)
        if pos is None:
            return []

        if pos.adopted:
            pos.bars_held += 1
            return []
```

The rest of the method (lines 27-66 in the original) is unchanged.

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_position_manager.py -v
```

Expected: ALL PASS, including the 6 new adopted-* tests.

- [ ] **Step 5: Run full test suite to confirm no regressions**

```bash
pytest -v
```

Expected: full suite passes.

- [ ] **Step 6: Commit**

```bash
git add core/position_manager.py tests/test_position_manager.py
git commit -m "feat(position-manager): skip lifecycle actions on adopted positions

Adopted positions lack the metadata (setup, initial_stop, bars_held
at adoption) needed to drive breakeven/time_stop honestly. The broker
bracket protects equity adoptions; crypto adoptions are flagged
elsewhere. Adopted positions are closed only by the reconciler when
Alpaca reports them gone."
```

---

## Task 3: Add `list_orders` to `AlpacaClient`

**Files:**
- Modify: `broker/alpaca_client.py:209` (insert after `get_assets`)
- Test: `tests/test_alpaca_client_list_orders.py` (new)

- [ ] **Step 1: Inspect existing test patterns for AlpacaClient**

Run:

```bash
ls tests/test_alpaca_client*.py 2>/dev/null
grep -l "AlpacaClient" tests/*.py | head -5
```

If a `tests/test_alpaca_client*.py` exists, follow its mocking pattern. Otherwise, this task creates the first one. The AlpacaClient uses `requests`; tests should mock `requests.Session.request` or `_request` directly.

- [ ] **Step 2: Write failing tests**

Create `tests/test_alpaca_client_list_orders.py`:

```python
from unittest.mock import MagicMock
import pytest
from broker.alpaca_client import AlpacaClient


def _make_client_with_response(payload):
    client = AlpacaClient.__new__(AlpacaClient)  # bypass network init
    fake_response = MagicMock()
    fake_response.json.return_value = payload
    fake_response.status_code = 200
    client._request = MagicMock(return_value=fake_response)
    return client


def test_list_orders_default_status_open():
    client = _make_client_with_response([])
    client.list_orders()
    args, kwargs = client._request.call_args
    assert args[0] == "GET"
    assert args[1] == "/v2/orders"
    assert kwargs["params"]["status"] == "open"
    assert kwargs["params"]["nested"] == "true"
    assert "symbols" not in kwargs["params"]


def test_list_orders_with_symbols_filter():
    client = _make_client_with_response([])
    client.list_orders(symbols=["AAPL", "MSFT"])
    _, kwargs = client._request.call_args
    assert kwargs["params"]["symbols"] == "AAPL,MSFT"


def test_list_orders_status_override():
    client = _make_client_with_response([])
    client.list_orders(status="closed")
    _, kwargs = client._request.call_args
    assert kwargs["params"]["status"] == "closed"


def test_list_orders_nested_false():
    client = _make_client_with_response([])
    client.list_orders(nested=False)
    _, kwargs = client._request.call_args
    assert kwargs["params"]["nested"] == "false"


def test_list_orders_returns_parsed_json():
    payload = [{"id": "o1", "symbol": "AAPL", "type": "limit"}]
    client = _make_client_with_response(payload)
    result = client.list_orders()
    assert result == payload
```

- [ ] **Step 3: Run tests to verify they fail**

```bash
pytest tests/test_alpaca_client_list_orders.py -v
```

Expected: FAIL — `AlpacaClient` has no `list_orders` attribute.

- [ ] **Step 4: Add `list_orders` method**

In `broker/alpaca_client.py`, insert after the `get_assets` method (after line 217, before `get_stock_snapshots`):

```python
    def list_orders(
        self,
        *,
        status: str = "open",
        symbols: list[str] | None = None,
        nested: bool = True,
    ) -> list[dict]:
        """GET /v2/orders — list orders, optionally filtered by status and symbols.

        nested=True returns child legs of bracket orders inside the parent's
        `legs` field; orphaned children whose parent has filled appear as
        top-level orders with `parent_id` set.
        """
        params: dict = {"status": status, "nested": "true" if nested else "false"}
        if symbols:
            params["symbols"] = ",".join(symbols)
        response = self._request("GET", "/v2/orders", params=params)
        return response.json()
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
pytest tests/test_alpaca_client_list_orders.py -v
```

Expected: ALL 5 PASS.

- [ ] **Step 6: Commit**

```bash
git add broker/alpaca_client.py tests/test_alpaca_client_list_orders.py
git commit -m "feat(broker): add AlpacaClient.list_orders for bracket recovery

Reconciler will use this to recover stop/target/stop_order_id from
the still-alive bracket children of orphaned equity positions."
```

---

## Task 4: Reconciler helpers — asset class, side, bracket-child indexer

**Files:**
- Create: `state/reconciler.py` (helpers only — class added in Task 5)
- Test: `tests/test_reconciler.py`

- [ ] **Step 1: Create skeleton `state/reconciler.py` with three pure helpers**

Create `state/reconciler.py`:

```python
from __future__ import annotations
from typing import Iterable


def _normalize_asset_class(raw: str | None) -> str | None:
    """Map Alpaca's asset_class strings to the codebase's canonical names.

    "us_equity" -> "equity"
    "crypto"    -> "crypto"
    anything else -> None (caller logs and skips)
    """
    if raw is None:
        return None
    s = raw.strip().lower()
    if s == "us_equity":
        return "equity"
    if s == "crypto":
        return "crypto"
    return None


def _normalize_side(raw: str) -> str:
    """Alpaca position side is 'long' or 'short' — pass through, defended."""
    s = (raw or "").strip().lower()
    if s in ("long", "short"):
        return s
    raise ValueError(f"Unexpected position side: {raw!r}")


def _index_bracket_children(orders: Iterable[dict]) -> dict[str, dict]:
    """Index open bracket children by symbol from a flat list of orders.

    Handles two shapes: (a) parent order with legs nested under `legs`,
    (b) orphaned children appearing as top-level orders with `parent_id` set.

    Returns: {symbol: {"stop": leg_dict | None, "target": leg_dict | None}}
    """
    out: dict[str, dict] = {}

    def _classify(order: dict) -> tuple[str | None, dict] | None:
        otype = (order.get("type") or "").lower()
        symbol = order.get("symbol")
        if symbol is None:
            return None
        if otype in ("stop", "stop_limit"):
            return ("stop", order)
        if otype == "limit" and order.get("limit_price") is not None:
            return ("target", order)
        return None

    for order in orders:
        legs = order.get("legs") or []
        candidates = list(legs) if legs else [order]
        for cand in candidates:
            classified = _classify(cand)
            if classified is None:
                continue
            kind, leg = classified
            symbol = leg["symbol"]
            slot = out.setdefault(symbol, {"stop": None, "target": None})
            if slot[kind] is None:
                slot[kind] = leg

    return out
```

- [ ] **Step 2: Write failing tests for the helpers**

Create `tests/test_reconciler.py`:

```python
import pytest
from state.reconciler import (
    _normalize_asset_class,
    _normalize_side,
    _index_bracket_children,
)


def test_normalize_asset_class_us_equity():
    assert _normalize_asset_class("us_equity") == "equity"


def test_normalize_asset_class_crypto():
    assert _normalize_asset_class("crypto") == "crypto"


def test_normalize_asset_class_uppercase():
    assert _normalize_asset_class("US_EQUITY") == "equity"


def test_normalize_asset_class_unknown_returns_none():
    assert _normalize_asset_class("forex") is None
    assert _normalize_asset_class("") is None
    assert _normalize_asset_class(None) is None


def test_normalize_side_long():
    assert _normalize_side("long") == "long"


def test_normalize_side_short():
    assert _normalize_side("short") == "short"


def test_normalize_side_uppercase():
    assert _normalize_side("LONG") == "long"


def test_normalize_side_unknown_raises():
    with pytest.raises(ValueError):
        _normalize_side("buy")


def test_index_bracket_children_nested_legs():
    parent = {
        "id": "p1", "symbol": "AAPL", "type": "limit", "side": "buy",
        "legs": [
            {"id": "stop1", "symbol": "AAPL", "type": "stop",
             "stop_price": "99.0", "side": "sell"},
            {"id": "tgt1", "symbol": "AAPL", "type": "limit",
             "limit_price": "102.0", "side": "sell"},
        ],
    }
    idx = _index_bracket_children([parent])
    assert idx["AAPL"]["stop"]["id"] == "stop1"
    assert idx["AAPL"]["target"]["id"] == "tgt1"


def test_index_bracket_children_orphaned_children():
    children = [
        {"id": "stop1", "symbol": "AAPL", "type": "stop_limit",
         "stop_price": "99.0", "parent_id": "p1", "side": "sell"},
        {"id": "tgt1", "symbol": "AAPL", "type": "limit",
         "limit_price": "102.0", "parent_id": "p1", "side": "sell"},
    ]
    idx = _index_bracket_children(children)
    assert idx["AAPL"]["stop"]["id"] == "stop1"
    assert idx["AAPL"]["target"]["id"] == "tgt1"


def test_index_bracket_children_only_stop_present():
    orders = [
        {"id": "stop1", "symbol": "AAPL", "type": "stop",
         "stop_price": "99.0", "side": "sell"},
    ]
    idx = _index_bracket_children(orders)
    assert idx["AAPL"]["stop"]["id"] == "stop1"
    assert idx["AAPL"]["target"] is None


def test_index_bracket_children_empty_input():
    assert _index_bracket_children([]) == {}


def test_index_bracket_children_ignores_unrelated_order_types():
    orders = [
        {"id": "m1", "symbol": "AAPL", "type": "market", "side": "buy"},
    ]
    assert _index_bracket_children(orders) == {}
```

- [ ] **Step 3: Run helper tests**

```bash
pytest tests/test_reconciler.py -v
```

Expected: ALL PASS (helpers were written in Step 1, tests follow them — this is the TDD inversion that makes sense for a new module: write the helper signatures, then validate them with tests).

If any fail, fix the helper, not the test.

- [ ] **Step 4: Commit**

```bash
git add state/reconciler.py tests/test_reconciler.py
git commit -m "feat(reconciler): add asset-class, side, and bracket-child helpers

Pure functions used by the upcoming Reconciler. Asset-class normalizes
Alpaca's 'us_equity' to the codebase canonical 'equity' (per memory
S20). Bracket-child indexer handles both nested-legs and
orphaned-child shapes returned by /v2/orders."
```

---

## Task 5: Reconciler core class

**Files:**
- Modify: `state/reconciler.py`
- Modify: `tests/test_reconciler.py`

- [ ] **Step 1: Write failing tests for `Reconciler.reconcile`**

Append to `tests/test_reconciler.py`:

```python
from datetime import datetime, timezone
import logging
from unittest.mock import MagicMock
from state.position_book import PositionBook, OpenPosition
from state.reconciler import Reconciler, ReconcileReport


def _trader_pos(symbol="AAPL", qty=10, side="long",
                stop=99.0, target=102.0, entry=100.0):
    return OpenPosition(
        symbol=symbol, setup="price_discovery", side=side, qty=qty,
        entry_px=entry, stop_px=stop, target_px=target,
        opened_at=datetime(2026, 5, 14, 14, 0, tzinfo=timezone.utc),
        order_id="o1", stop_order_id="leg1", initial_stop_px=stop,
    )


def _broker_position(symbol="AAPL", qty="10", side="long",
                     entry="100.0", asset_class="us_equity"):
    return {"symbol": symbol, "qty": qty, "side": side,
            "avg_entry_price": entry, "asset_class": asset_class}


def _fake_alpaca(positions=None, orders=None):
    alp = MagicMock()
    alp.get_positions.return_value = positions or []
    alp.list_orders.return_value = orders or []
    return alp


def test_reconcile_empty_book_empty_broker():
    book = PositionBook()
    r = Reconciler(_fake_alpaca(), ac_configs={})
    report = r.reconcile(book)
    assert isinstance(report, ReconcileReport)
    assert report.closed == []
    assert report.adopted_equity == []
    assert report.adopted_crypto == []
    assert report.drift == []
    assert report.equity_no_bracket == []


def test_reconcile_book_matches_broker_no_changes():
    book = PositionBook()
    book.add(_trader_pos("AAPL", qty=10))
    alp = _fake_alpaca(positions=[_broker_position("AAPL", qty="10")])
    r = Reconciler(alp, ac_configs={})
    report = r.reconcile(book)
    assert report.closed == []
    assert report.adopted_equity == []
    assert book.count() == 1
    assert book.get("AAPL").adopted is False  # untouched


def test_reconcile_closes_position_when_broker_says_gone():
    book = PositionBook()
    book.add(_trader_pos("AAPL"))
    alp = _fake_alpaca(positions=[])  # broker says nothing open
    r = Reconciler(alp, ac_configs={})
    report = r.reconcile(book)
    assert report.closed == ["AAPL"]
    assert book.get("AAPL") is None


def test_reconcile_adopts_equity_with_alive_bracket():
    book = PositionBook()
    bracket_parent = {
        "id": "p1", "symbol": "AAPL", "type": "limit", "side": "buy",
        "legs": [
            {"id": "stop1", "symbol": "AAPL", "type": "stop",
             "stop_price": "99.0", "side": "sell"},
            {"id": "tgt1", "symbol": "AAPL", "type": "limit",
             "limit_price": "102.0", "side": "sell"},
        ],
    }
    alp = _fake_alpaca(
        positions=[_broker_position("AAPL", qty="10", entry="100.0")],
        orders=[bracket_parent],
    )
    r = Reconciler(alp, ac_configs={})
    report = r.reconcile(book)
    assert report.adopted_equity == ["AAPL"]
    pos = book.get("AAPL")
    assert pos is not None
    assert pos.adopted is True
    assert pos.qty == 10.0
    assert pos.side == "long"
    assert pos.entry_px == 100.0
    assert pos.stop_px == 99.0
    assert pos.target_px == 102.0
    assert pos.stop_order_id == "stop1"
    assert pos.initial_stop_px == 99.0


def test_reconcile_adopts_equity_with_orphaned_bracket_children():
    book = PositionBook()
    children = [
        {"id": "stop1", "symbol": "AAPL", "type": "stop_limit",
         "stop_price": "99.0", "parent_id": "p1", "side": "sell"},
        {"id": "tgt1", "symbol": "AAPL", "type": "limit",
         "limit_price": "102.0", "parent_id": "p1", "side": "sell"},
    ]
    alp = _fake_alpaca(
        positions=[_broker_position("AAPL")],
        orders=children,
    )
    r = Reconciler(alp, ac_configs={})
    r.reconcile(book)
    pos = book.get("AAPL")
    assert pos.stop_order_id == "stop1"
    assert pos.stop_px == 99.0
    assert pos.target_px == 102.0


def test_reconcile_adopts_equity_no_bracket():
    book = PositionBook()
    alp = _fake_alpaca(
        positions=[_broker_position("AAPL", qty="10", entry="100.0")],
        orders=[],
    )
    r = Reconciler(alp, ac_configs={})
    report = r.reconcile(book)
    assert report.adopted_equity == ["AAPL"]
    assert report.equity_no_bracket == ["AAPL"]
    pos = book.get("AAPL")
    assert pos.stop_px is None
    assert pos.target_px is None
    assert pos.stop_order_id is None


def test_reconcile_adopts_crypto_no_stop():
    book = PositionBook()
    alp = _fake_alpaca(
        positions=[_broker_position("BTCUSD", qty="0.5",
                                    entry="50000.0", asset_class="crypto")],
        orders=[],
    )
    r = Reconciler(alp, ac_configs={})
    report = r.reconcile(book)
    assert report.adopted_crypto == ["BTCUSD"]
    pos = book.get("BTCUSD")
    assert pos.adopted is True
    assert pos.qty == 0.5
    assert pos.entry_px == 50_000.0
    assert pos.stop_px is None
    assert pos.target_px is None


def test_reconcile_logs_drift_no_mutation():
    book = PositionBook()
    book.add(_trader_pos("AAPL", qty=100))
    alp = _fake_alpaca(positions=[_broker_position("AAPL", qty="50")])
    r = Reconciler(alp, ac_configs={})
    report = r.reconcile(book)
    assert report.drift == [("AAPL", 100.0, 50.0)]
    assert book.get("AAPL").qty == 100  # unchanged


def test_reconcile_unknown_asset_class_skips_adoption(caplog):
    book = PositionBook()
    alp = _fake_alpaca(
        positions=[_broker_position("EURUSD", asset_class="forex")],
    )
    r = Reconciler(alp, ac_configs={})
    with caplog.at_level(logging.WARNING):
        report = r.reconcile(book)
    assert report.adopted_equity == []
    assert report.adopted_crypto == []
    assert book.get("EURUSD") is None
    assert any("RECONCILE_UNKNOWN_ASSET_CLASS" in rec.message
               for rec in caplog.records)


def test_reconcile_short_position_qty_uses_abs():
    book = PositionBook()
    alp = _fake_alpaca(
        positions=[_broker_position("AAPL", qty="-10", side="short")],
    )
    r = Reconciler(alp, ac_configs={})
    r.reconcile(book)
    pos = book.get("AAPL")
    assert pos.side == "short"
    assert pos.qty == 10.0  # absolute value


def test_reconcile_naked_crypto_logs_every_cycle(caplog):
    book = PositionBook()
    alp = _fake_alpaca(
        positions=[_broker_position("BTCUSD", asset_class="crypto",
                                    qty="0.5", entry="50000.0")],
    )
    r = Reconciler(alp, ac_configs={})
    with caplog.at_level(logging.WARNING):
        r.reconcile(book)  # adopts + logs RECONCILE_ADOPTED_CRYPTO_NO_STOP + ADOPTED_CRYPTO_NAKED
        caplog.clear()
        r.reconcile(book)  # second cycle: position already adopted, only ADOPTED_CRYPTO_NAKED fires
    assert any("ADOPTED_CRYPTO_NAKED" in rec.message
               for rec in caplog.records)


def test_reconcile_does_not_double_log_adoption_for_existing_adopted_position():
    book = PositionBook()
    alp = _fake_alpaca(
        positions=[_broker_position("AAPL", qty="10")],
    )
    r = Reconciler(alp, ac_configs={})
    r.reconcile(book)
    report2 = r.reconcile(book)
    assert report2.adopted_equity == []  # already in book, not adopted again
    assert book.count() == 1
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_reconciler.py -v
```

Expected: 11 new tests FAIL — `Reconciler` and `ReconcileReport` don't exist yet.

- [ ] **Step 3: Add `ReconcileReport` and `Reconciler` to `state/reconciler.py`**

Append to `state/reconciler.py` (after the helpers from Task 4):

```python
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from state.position_book import OpenPosition, PositionBook


@dataclass
class ReconcileReport:
    closed: list[str] = field(default_factory=list)
    adopted_equity: list[str] = field(default_factory=list)
    adopted_crypto: list[str] = field(default_factory=list)
    drift: list[tuple[str, float, float]] = field(default_factory=list)
    equity_no_bracket: list[str] = field(default_factory=list)


_QTY_EPS = 1e-6


class Reconciler:
    """Reconciles the in-memory PositionBook against Alpaca's /v2/positions.

    Policy (see spec 2026-05-22-broker-position-reconciliation-design.md):
    - Closed (in book, not in broker): book.close(symbol).
    - Drift (qty differs): log only, no mutation.
    - Orphan (in broker, not in book): adopt as monitor-only with
      adopted=True. Equity adoptions recover stop/target/stop_order_id from
      the live bracket children (or live with all three None and a
      RECONCILE_EQUITY_NO_BRACKET warning). Crypto adoptions are naked.
    """

    def __init__(self, alpaca, ac_configs: dict | None = None,
                 *, logger: logging.Logger | None = None) -> None:
        self._alpaca = alpaca
        self._ac_configs = ac_configs or {}
        self._log = logger or logging.getLogger("vwap_wave.reconciler")

    def reconcile(self, book: PositionBook) -> ReconcileReport:
        report = ReconcileReport()

        broker_positions = self._alpaca.get_positions()
        broker_by_symbol: dict[str, dict] = {
            p["symbol"]: p for p in broker_positions
        }

        # 1. Closed: in book, not in broker.
        for symbol in list(book.symbols()):
            if symbol not in broker_by_symbol:
                pos = book.close(symbol)
                report.closed.append(symbol)
                self._log.info(
                    "RECONCILE_CLOSED symbol=%s adopted=%s setup=%s",
                    symbol,
                    getattr(pos, "adopted", "?"),
                    getattr(pos, "setup", "?"),
                )

        # 2. Drift: in both, qty differs (log only).
        for symbol, broker_pos in broker_by_symbol.items():
            local_pos = book.get(symbol)
            if local_pos is None:
                continue
            broker_qty = abs(float(broker_pos["qty"]))
            if abs(local_pos.qty - broker_qty) > _QTY_EPS:
                report.drift.append((symbol, local_pos.qty, broker_qty))
                self._log.warning(
                    "RECONCILE_DRIFT symbol=%s book_qty=%s broker_qty=%s",
                    symbol, local_pos.qty, broker_qty,
                )

        # 3. Orphans: in broker, not in book → adopt by asset class.
        orphan_equity_symbols: list[str] = []
        orphan_crypto_records: list[dict] = []

        for symbol, broker_pos in broker_by_symbol.items():
            if book.get(symbol) is not None:
                continue
            ac = _normalize_asset_class(broker_pos.get("asset_class"))
            if ac == "equity":
                orphan_equity_symbols.append(symbol)
            elif ac == "crypto":
                orphan_crypto_records.append(broker_pos)
            else:
                self._log.warning(
                    "RECONCILE_UNKNOWN_ASSET_CLASS symbol=%s class=%s",
                    symbol, broker_pos.get("asset_class"),
                )

        # 3a. Equity orphans: one batched list_orders call to recover brackets.
        bracket_index: dict[str, dict] = {}
        if orphan_equity_symbols:
            try:
                open_orders = self._alpaca.list_orders(
                    status="open",
                    symbols=orphan_equity_symbols,
                    nested=True,
                )
                bracket_index = _index_bracket_children(open_orders)
            except Exception as exc:
                self._log.error(
                    "RECONCILE_LIST_ORDERS_FAILED — adopting orphans without bracket data: %s",
                    exc, exc_info=True,
                )
                bracket_index = {}

        for symbol in orphan_equity_symbols:
            broker_pos = broker_by_symbol[symbol]
            legs = bracket_index.get(symbol, {})
            stop_leg = legs.get("stop")
            target_leg = legs.get("target")
            stop_px = float(stop_leg["stop_price"]) if stop_leg else None
            target_px = (float(target_leg["limit_price"])
                         if target_leg else None)
            stop_order_id = stop_leg["id"] if stop_leg else None
            if stop_leg is None and target_leg is None:
                report.equity_no_bracket.append(symbol)
                self._log.warning(
                    "RECONCILE_EQUITY_NO_BRACKET symbol=%s qty=%s entry=%s",
                    symbol, broker_pos["qty"],
                    broker_pos["avg_entry_price"],
                )
            pos = OpenPosition(
                symbol=symbol,
                setup="adopted",
                side=_normalize_side(broker_pos["side"]),
                qty=abs(float(broker_pos["qty"])),
                entry_px=float(broker_pos["avg_entry_price"]),
                stop_px=stop_px,
                target_px=target_px,
                opened_at=datetime.now(timezone.utc),
                order_id="",
                stop_order_id=stop_order_id,
                initial_stop_px=stop_px,
                adopted=True,
            )
            book.add(pos)
            report.adopted_equity.append(symbol)
            self._log.info(
                "RECONCILE_ADOPTED_EQUITY symbol=%s side=%s qty=%s entry=%s "
                "stop=%s target=%s stop_leg=%s",
                symbol, pos.side, pos.qty, pos.entry_px,
                pos.stop_px, pos.target_px, pos.stop_order_id,
            )

        # 3b. Crypto orphans: naked, loud warning.
        for broker_pos in orphan_crypto_records:
            symbol = broker_pos["symbol"]
            pos = OpenPosition(
                symbol=symbol,
                setup="adopted",
                side=_normalize_side(broker_pos["side"]),
                qty=abs(float(broker_pos["qty"])),
                entry_px=float(broker_pos["avg_entry_price"]),
                stop_px=None,
                target_px=None,
                opened_at=datetime.now(timezone.utc),
                order_id="",
                stop_order_id=None,
                initial_stop_px=None,
                adopted=True,
            )
            book.add(pos)
            report.adopted_crypto.append(symbol)
            self._log.warning(
                "RECONCILE_ADOPTED_CRYPTO_NO_STOP symbol=%s side=%s qty=%s entry=%s",
                symbol, pos.side, pos.qty, pos.entry_px,
            )

        # 4. Recurring naked-crypto warning (every cycle).
        for pos in book.all():
            if pos.adopted and pos.stop_px is None:
                self._log.warning(
                    "ADOPTED_CRYPTO_NAKED symbol=%s qty=%s entry=%s — manual close required",
                    pos.symbol, pos.qty, pos.entry_px,
                )

        return report
```

- [ ] **Step 4: Run reconciler tests**

```bash
pytest tests/test_reconciler.py -v
```

Expected: ALL PASS, including the 11 new `Reconciler` tests.

- [ ] **Step 5: Run full test suite to confirm no regressions**

```bash
pytest -v
```

Expected: full suite passes.

- [ ] **Step 6: Commit**

```bash
git add state/reconciler.py tests/test_reconciler.py
git commit -m "feat(reconciler): add Reconciler class for broker-vs-book sync

Detects closed positions (book.close), drift (log only), and orphans
(adopt as monitor-only). Equity adoptions recover stop/target/leg-id
from live bracket children via list_orders. Crypto adoptions are
naked and logged loudly each cycle."
```

---

## Task 6: Wire reconciler into `main.py` (startup + per-cycle)

**Files:**
- Modify: `main.py:281` (startup wiring after `book = PositionBook()`)
- Modify: `main.py:349-364` (per-cycle wiring before `engine.tick`)

- [ ] **Step 1: Verify the file structure of main.py around the targets**

```bash
sed -n '275,295p' main.py
sed -n '345,385p' main.py
```

Confirm line numbers below match. If the file has shifted, adjust.

- [ ] **Step 2: Add startup wiring**

Edit `main.py`. After `book = PositionBook()` (currently line 281) and the existing `account = alpaca.get_account()` block, insert the reconciler instantiation and startup call. Specifically, replace the block:

```python
    book = PositionBook()

    account = alpaca.get_account()
    initial_equity = float(account.get("equity") or account.get("portfolio_value") or 0)
    if initial_equity <= 0:
        logger.error("Account returned non-positive equity; aborting")
        sys.exit(1)
    ledger = DailyLedger(initial_equity=initial_equity)
```

with:

```python
    book = PositionBook()

    account = alpaca.get_account()
    initial_equity = float(account.get("equity") or account.get("portfolio_value") or 0)
    if initial_equity <= 0:
        logger.error("Account returned non-positive equity; aborting")
        sys.exit(1)
    ledger = DailyLedger(initial_equity=initial_equity)

    reconciler = Reconciler(alpaca, ac_configs)
    try:
        startup_report = reconciler.reconcile(book)
    except Exception as exc:
        logger.error("RECONCILE_STARTUP_FAILED: %s", exc, exc_info=True)
        sys.exit(1)
    logger.info(
        "RECONCILE_STARTUP closed=%d adopted_eq=%d adopted_cr=%d drift=%d no_bracket=%d",
        len(startup_report.closed), len(startup_report.adopted_equity),
        len(startup_report.adopted_crypto), len(startup_report.drift),
        len(startup_report.equity_no_bracket),
    )
```

Then add the import at the top of `main.py` (group with the other `state.` imports):

```python
from state.reconciler import Reconciler
```

- [ ] **Step 3: Add per-cycle wiring before `engine.tick`**

In the main loop body, currently:

```python
        try:
            cycle_now = datetime.now(timezone.utc)
            executor.reset_cycle()
            fresh_bars: dict[str, list] = {}
            for sym, ac_name in symbols:
                ...
                if new_bars:
                    fresh_bars[sym] = new_bars
            engine.tick(now=cycle_now, fresh_bars=fresh_bars)
```

Insert the per-cycle reconcile immediately before `engine.tick(...)`:

```python
        try:
            cycle_now = datetime.now(timezone.utc)
            executor.reset_cycle()
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

            try:
                cycle_report = reconciler.reconcile(book)
                if (cycle_report.closed or cycle_report.adopted_equity
                        or cycle_report.adopted_crypto or cycle_report.drift):
                    logger.info(
                        "RECONCILE closed=%d adopted_eq=%d adopted_cr=%d drift=%d",
                        len(cycle_report.closed),
                        len(cycle_report.adopted_equity),
                        len(cycle_report.adopted_crypto),
                        len(cycle_report.drift),
                    )
            except Exception as exc:
                logger.error("RECONCILE_ERROR: %s", exc, exc_info=True)

            engine.tick(now=cycle_now, fresh_bars=fresh_bars)
```

(Only the inner `try/except` reconcile block and the `engine.tick(...)` ordering changes; the rest of the cycle is identical.)

- [ ] **Step 4: Run the full test suite**

```bash
pytest -v
```

Expected: all tests pass. `main.py` itself isn't directly unit-tested, so coverage of the wiring comes from manual smoke + the unit tests on the reconciler.

- [ ] **Step 5: Static smoke — make sure `main.py` parses and imports cleanly**

```bash
python -c "import main"
```

Expected: silent success (no syntax error, no missing import).

- [ ] **Step 6: Manual paper-account smoke (operator-driven, deferred)**

The operator runs:

```bash
python main.py
```

against the paper account that currently holds the 4 open positions. Expected first log within seconds of startup:

```
INFO RECONCILE_STARTUP closed=0 adopted_eq=4 adopted_cr=0 drift=0 no_bracket=0
INFO RECONCILE_ADOPTED_EQUITY symbol=… side=long qty=… entry=… stop=… target=… stop_leg=…
... (one per adopted position)
```

Expected first `CYCLE_DONE` line: `open_positions=4` (matching Alpaca).

Document the smoke result (positions count, any unexpected log lines) before claiming the bug is fixed.

- [ ] **Step 7: Commit**

```bash
git add main.py
git commit -m "feat(main): reconcile PositionBook against Alpaca on startup and each cycle

Fixes the 'open_positions=0 with 4 broker positions' bug. Startup
reconcile fail-starts if it raises; per-cycle reconcile logs and
continues. Adopted positions appear in book.count() and are excluded
from PositionManager lifecycle actions."
```

---

## Self-Review

**Spec coverage:**
- §Architecture (new module, startup+cycle wiring): Tasks 5 + 6 ✓
- §OpenPosition schema: Task 1 ✓
- §PositionManager skip-adopted: Task 2 ✓
- §AlpacaClient.list_orders: Task 3 ✓
- §Reconciliation Algorithm (closed / drift / orphan-equity / orphan-crypto / naked-warning): Task 5 ✓
- §Asset-class & side normalization: Task 4 ✓
- §Bracket child indexing: Task 4 ✓
- §Error Handling table (startup fail-fast, cycle log-and-continue, list_orders failure → adopt without bracket data): Tasks 5–6 ✓
- §Testing (15 listed tests across 3 files): Tasks 1, 2, 5 cover all 15 ✓
- §Files Touched table: each file referenced in a task ✓

**Placeholder scan:** no TBD/TODO; every code step contains the actual code; every command has expected output.

**Type consistency:** `Reconciler` constructor signature matches across Tasks 5 and 6 (`Reconciler(alpaca, ac_configs, *, logger=None)`). `ReconcileReport` field names (`closed`, `adopted_equity`, `adopted_crypto`, `drift`, `equity_no_bracket`) consistent across spec, Task 5, and Task 6 logging. `OpenPosition.adopted` referenced consistently. `_index_bracket_children` returns the shape (`{"stop": leg | None, "target": leg | None}`) used by Task 5.

---

## Execution

Plan complete and saved to `docs/superpowers/plans/2026-05-22-broker-position-reconciliation.md`.
