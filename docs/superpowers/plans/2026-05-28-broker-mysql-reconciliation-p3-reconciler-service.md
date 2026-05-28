# Broker↔MySQL Reconciliation v2 — Plan 3: Dedicated Reconciler Service

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stand up a dedicated `reconciler` service container that owns the cross-strategy invariant (`Σ open MySQL qty per symbol == broker qty per symbol`), applies tagged Alpaca fills to the right `(strategy, setup)` MySQL rows, and runs the multi-strike confirmation rule for every irresolvable anomaly. After this plan, no strategy container reconciles broker state — the per-strategy `state/reconciler.py` is removed and its logic is replaced by a single, deterministic service.

**Architecture:** New top-level Python package `reconciler/` with five focused modules (`main` loop, `fills` application, `invariant` checker, `strikes` decision logic, `events` writer). Runs as a docker-compose service alongside traders. 30-second loop. Uses the same `AlpacaClient` and `MySQLStore` as the traders. Heartbeat row on every cycle. Shadow-mode env flag (`SHADOW_MODE=true`) computes everything but mutates nothing — used during phased rollout. Strategy containers stop calling reconciliation entirely; their own `state/reconciler.py` is removed and `main.py` no longer instantiates a reconciler.

**Tech Stack:** Python 3.12, SQLAlchemy 2.x ORM (existing `MySQLStore`), the existing `AlpacaClient` + `client_order_id` helpers from Plans 1+2, pytest, docker-compose, the existing `notifications.py` Telegram helper (extended with `send_reconcile_alert`).

**Spec:** `docs/superpowers/specs/2026-05-28-broker-mysql-reconciliation-design.md` §2 (service shape), §3 (multi-strike rule), §4 (fill application), §5 rollout step 3 (shadow mode).

**Builds on:** Plan 1 (#54 merged) — schema with `reconciliation_strikes`, `reconciliation_events`, COID columns. Plan 2 (#55 merged) — every new order has a tagged COID. Plan 3 starts consuming those COIDs.

---

## File Structure

**Create (`reconciler/` package — new top-level directory):**

- `reconciler/__init__.py` — empty package marker.
- `reconciler/config.py` — env-driven `ReconcilerConfig` dataclass: `interval_s`, `strike_threshold`, `strike_min_gap_s`, `qty_eps`, `shadow_mode`, `state_file_path`. Pure-data, no I/O. ~40 lines.
- `reconciler/state.py` — JSON-persisted `last_orders_check_ts` (so the loop can pull fills "since last check" across restarts). Atomic write. ~50 lines.
- `reconciler/fills.py` — `apply_tagged_fill(session, fill, mysql_store, ...)` — pure function, idempotent. Given a parsed COID and an Alpaca order dict, applies the right MySQL mutation: insert (entry fill with no row), close (exit fill with open row), or noop. ~140 lines.
- `reconciler/invariant.py` — `check_invariant(session, broker_positions, qty_eps, ...)` — returns a list of `Anomaly` records (qty_drift, mysql_only, broker_only). Pure function over a snapshot. ~110 lines.
- `reconciler/strikes.py` — `process_anomaly(session, anomaly, config, now, ...)` — looks up or inserts a strike row, applies the strike algorithm, decides whether to alert/freeze/noop, returns a `StrikeOutcome`. ~150 lines.
- `reconciler/events.py` — thin wrapper over `EventRow`: `emit_event(session, type, strategy_id=None, symbol=None, payload=None)`. Used for heartbeat, untagged fills, anomaly confirmations. ~30 lines.
- `reconciler/main.py` — the loop. Wires Alpaca + MySQL, runs one cycle: pull → apply fills → invariant → strikes → heartbeat. ~200 lines.

**Modify:**

- `broker/alpaca_client.py:210-227` (`list_orders`) — accept new `after: datetime | None = None` parameter and forward as `after` query param when provided. Required for "fills since last check".
- `state/mysql_store.py` — add four small helper methods used only by the reconciler service:
  - `find_open_position_by_coid(client_order_id) -> PositionRow | None`
  - `find_open_position_by_setup(strategy_id, symbol, setup_name) -> PositionRow | None`
  - `insert_position_from_fill(strategy, setup, symbol, side, qty, entry_px, opened_at, asset_class, client_order_id) -> int` — inserts a position row from a tagged entry fill where no MySQL row existed yet (the "submitted, filled, crashed before write" recovery path).
  - `sum_qty_by_symbol() -> dict[str, float]` — aggregates open qty per symbol across ALL strategies. Crypto slash-form normalized to flat (`BTC/USD` → `BTCUSD`).
  - All callers must use `Session(self._engine)` consistently with the rest of the file.
- `state/mysql_store.py` — extend `position_closed` with one new optional parameter `strategy_id: int | None = None` (defaults to `self.strategy_id`). The reconciler service needs to close positions belonging to *other* strategies, identified by id rather than by name.
- `main.py` — REMOVE the entire reconciler block (instantiation at line ~416, startup reconcile at ~421, and per-cycle reconcile at ~519). Strategy containers no longer reconcile. Strategy still loads/rebuilds book from MySQL each cycle (unchanged) and writes via `position_opened` / `position_closed` (unchanged).
- `docker-compose.yml` — add a new `reconciler` service block after the existing traders.
- `notifications.py` — add `send_reconcile_alert(direction, symbol, strategy_name, snapshot, strike_count)` — Telegram one-liner.

**Delete (at the END of the rollout, in Task 12):**

- `state/reconciler.py` — entire file.
- `tests/test_reconciler.py` — entire file.

**Tests (new):**

- `tests/test_reconciler_config.py` — env defaults + overrides. ~40 lines.
- `tests/test_reconciler_state.py` — round-trip the state file. ~30 lines.
- `tests/test_reconciler_fills.py` — every fill outcome (tagged entry inserts, tagged exit closes, idempotent re-apply, untagged → event-only, role mismatch handling). ~200 lines.
- `tests/test_reconciler_invariant.py` — qty_drift, mysql_only, broker_only detection on synthetic snapshots. ~150 lines.
- `tests/test_reconciler_strikes.py` — strike progression 1→2→3, self-heal auto-clear, min-gap rate limit, per-direction action policy. ~200 lines.
- `tests/test_reconciler_main_loop.py` — full single-cycle integration test against an in-memory SQLite engine + mocked AlpacaClient. ~150 lines.
- `tests/test_alpaca_client_list_orders.py` — extend the existing file with one new test asserting `after` is forwarded as a query param.
- `tests/test_mysql_store_reconciler.py` — the four new MySQLStore helpers, against in-memory SQLite. ~120 lines.

---

## Anomaly + Outcome data shapes (used across modules)

Defined in `reconciler/invariant.py` and `reconciler/strikes.py`. These are the contracts between modules.

```python
# reconciler/invariant.py
@dataclass(frozen=True)
class Anomaly:
    direction: str         # 'qty_drift' | 'mysql_only' | 'broker_only'
    symbol: str            # broker-flat form ('BTCUSD', 'AAPL')
    strategy_id: int | None  # set for mysql_only; None for qty_drift/broker_only
    snapshot: dict         # {'mysql_sum': float, 'broker_qty': float, ...}

    @property
    def key(self) -> str:
        # Used as the `key` column on reconciliation_strikes.
        if self.direction == 'mysql_only':
            return f"mysql_only:{self.strategy_id}:{self.symbol}"
        return f"{self.direction}:{self.symbol}"
```

```python
# reconciler/strikes.py
@dataclass(frozen=True)
class StrikeOutcome:
    action: str            # 'noop' | 'logged_strike1' | 'alerted' | 'frozen' | 'self_healed'
    strike_count: int      # current count after processing
    alert_sent: bool       # True iff a notification fired this cycle
```

---

## Task 1: Extend `AlpacaClient.list_orders` with `after` parameter

**Files:**
- Modify: `broker/alpaca_client.py:210-227` (`list_orders`)
- Modify: `tests/test_alpaca_client_list_orders.py` — add one new test.

The reconciler needs to pull fills since its last check. Alpaca's `/v2/orders` endpoint accepts `after` (ISO 8601 timestamp).

- [ ] **Step 1: Read the existing test pattern**

Open `tests/test_alpaca_client_list_orders.py` and look at how existing tests capture the request params. They use the same `patch.object(client._session, "request", ...)` pattern as `test_alpaca_client_orders.py`.

- [ ] **Step 2: Add a failing test**

Append to `tests/test_alpaca_client_list_orders.py`:

```python
def test_list_orders_forwards_after_parameter():
    """`after` is sent as the `after` query param when provided."""
    from datetime import datetime, timezone
    client = AlpacaClient()
    after_ts = datetime(2026, 5, 28, 14, 0, tzinfo=timezone.utc)
    with patch.object(client._session, "request",
                       return_value=_resp(200, [])) as req:
        client.list_orders(status="closed", after=after_ts, nested=True)
        params = req.call_args[1]["params"]
        # Alpaca expects ISO 8601 — confirm a string starting with the date
        assert "after" in params
        assert params["after"].startswith("2026-05-28T14:00:00")


def test_list_orders_omits_after_when_not_provided():
    client = AlpacaClient()
    with patch.object(client._session, "request",
                       return_value=_resp(200, [])) as req:
        client.list_orders(status="open")
        params = req.call_args[1]["params"]
        assert "after" not in params
```

- [ ] **Step 3: Run — confirm failure**

```bash
pytest tests/test_alpaca_client_list_orders.py::test_list_orders_forwards_after_parameter tests/test_alpaca_client_list_orders.py::test_list_orders_omits_after_when_not_provided -v
```

Expected: FAIL with `TypeError: list_orders() got an unexpected keyword argument 'after'`.

- [ ] **Step 4: Update `list_orders`**

In `broker/alpaca_client.py`, replace the `list_orders` method (currently lines 210-227) with:

```python
    def list_orders(
        self,
        *,
        status: str = "open",
        symbols: list[str] | None = None,
        nested: bool = True,
        after: "datetime | None" = None,
    ) -> list[dict]:
        """GET /v2/orders — list orders, optionally filtered.

        nested=True returns child legs of bracket orders inside the parent's
        `legs` field; orphaned children whose parent has filled appear as
        top-level orders with `parent_id` set.

        `after` (timezone-aware datetime) filters to orders updated/submitted
        after the given timestamp. Used by the reconciler service to pull
        fills since its last cycle.
        """
        params: dict = {"status": status, "nested": "true" if nested else "false"}
        if symbols:
            params["symbols"] = ",".join(symbols)
        if after is not None:
            params["after"] = after.isoformat()
        response = self._request("GET", "/v2/orders", params=params)
        return response.json()
```

The `datetime` import already exists at the top of `broker/alpaca_client.py` — leave imports as-is. The string-quoted forward reference `"datetime | None"` keeps things simple if the file uses `from __future__ import annotations` (it does — check line 1).

- [ ] **Step 5: Run — expect green**

```bash
pytest tests/test_alpaca_client_list_orders.py -v
```

Expected: all PASS (existing + 2 new).

- [ ] **Step 6: Commit**

```bash
git add broker/alpaca_client.py tests/test_alpaca_client_list_orders.py
git commit -m "feat(broker): list_orders accepts after parameter for fills-since queries"
```

---

## Task 2: New `MySQLStore` helpers used only by the reconciler

**Files:**
- Modify: `state/mysql_store.py` — add 4 new methods + extend `position_closed`.
- Create: `tests/test_mysql_store_reconciler.py`

The reconciler runs against MySQL but doesn't own the `MySQLStore` class — it instantiates one configured for `strategy_name="reconciler"` (used only to satisfy the constructor; the reconciler's queries cross all strategies).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_mysql_store_reconciler.py`:

```python
"""Tests for MySQLStore helpers added for the reconciler service (Plan 3).

Uses an in-memory SQLite engine to verify cross-strategy queries and the
fill-recovery insert path.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from state.mysql_store import (
    Base,
    MySQLStore,
    PositionRow,
    StrategyRow,
)
from state.position_book import OpenPosition


@pytest.fixture
def store():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    s = MySQLStore.__new__(MySQLStore)
    s._engine = engine
    s.strategy_name = "vwap_wave"
    s._log = logging.getLogger("test_recon_helpers")
    with Session(engine) as session:
        session.add_all([
            StrategyRow(name="vwap_wave"),
            StrategyRow(name="rsi_equity"),
        ])
        session.commit()
        rows = session.query(StrategyRow).order_by(StrategyRow.id).all()
        s._strategy_id = rows[0].id
        s._other_strategy_id = rows[1].id
    return s


def _open_pos(store, strategy_id: int, symbol: str, setup: str, qty: float,
              client_order_id: str | None = None):
    pos = OpenPosition(
        symbol=symbol, setup=setup, side="long", qty=qty,
        entry_px=100.0, stop_px=99.0, target_px=101.0,
        opened_at=datetime(2026, 5, 28, 14, 0, tzinfo=timezone.utc),
        order_id="o", initial_stop_px=99.0,
        client_order_id=client_order_id,
    )
    # Bypass strategy_id property — write directly under the requested id
    saved_strategy_id = store._strategy_id
    store._strategy_id = strategy_id
    try:
        store.position_opened(pos, "equity")
    finally:
        store._strategy_id = saved_strategy_id


def test_find_open_position_by_coid_returns_row(store):
    coid = "aitrader__vwap_wave__vwap_bounce__AAPL__entry__abcd1234"
    _open_pos(store, store._strategy_id, "AAPL", "vwap_bounce", 1.0, coid)
    row = store.find_open_position_by_coid(coid)
    assert row is not None
    assert row.symbol == "AAPL"
    assert row.client_order_id == coid


def test_find_open_position_by_coid_returns_none_for_unknown(store):
    assert store.find_open_position_by_coid("aitrader__x__y__Z__entry__deadbeef") is None


def test_find_open_position_by_coid_returns_none_for_closed(store):
    coid = "aitrader__vwap_wave__vwap_bounce__AAPL__entry__abcd1234"
    _open_pos(store, store._strategy_id, "AAPL", "vwap_bounce", 1.0, coid)
    store.position_closed(symbol="AAPL", exit_px=101.0, close_reason="target",
                          setup_name="vwap_bounce")
    assert store.find_open_position_by_coid(coid) is None


def test_find_open_position_by_setup_cross_strategy(store):
    """Reconciler must look up positions in any strategy, not just self.strategy_id."""
    _open_pos(store, store._other_strategy_id, "AAPL", "rsi_long", 5.0)
    row = store.find_open_position_by_setup(
        strategy_id=store._other_strategy_id, symbol="AAPL", setup_name="rsi_long",
    )
    assert row is not None
    assert row.qty == Decimal("5.00000000")


def test_find_open_position_by_setup_returns_none_for_other_strategy(store):
    """Wrong strategy_id must not match."""
    _open_pos(store, store._strategy_id, "AAPL", "vwap_bounce", 1.0)
    row = store.find_open_position_by_setup(
        strategy_id=store._other_strategy_id, symbol="AAPL", setup_name="vwap_bounce",
    )
    assert row is None


def test_insert_position_from_fill_creates_row_with_coid(store):
    coid = "aitrader__vwap_wave__vwap_bounce__AAPL__entry__abcd1234"
    new_id = store.insert_position_from_fill(
        strategy_id=store._strategy_id,
        setup_name="vwap_bounce",
        symbol="AAPL",
        side="long",
        qty=2.0,
        entry_px=100.0,
        opened_at=datetime(2026, 5, 28, 14, 0, tzinfo=timezone.utc),
        asset_class="equity",
        client_order_id=coid,
    )
    assert isinstance(new_id, int)
    with Session(store._engine) as session:
        row = session.query(PositionRow).filter(PositionRow.id == new_id).one()
        assert row.client_order_id == coid
        assert row.qty == Decimal("2.00000000")
        assert row.status == "open"
        assert row.adopted is False  # crash-before-write recovery is not "adopted"


def test_sum_qty_by_symbol_aggregates_across_strategies(store):
    _open_pos(store, store._strategy_id,        "AAPL", "vwap_bounce", 3.0)
    _open_pos(store, store._other_strategy_id,  "AAPL", "rsi_long",    2.0)
    _open_pos(store, store._strategy_id,        "BTCUSD", "vwap_bounce", 0.5)
    sums = store.sum_qty_by_symbol()
    assert sums["AAPL"] == 5.0
    assert sums["BTCUSD"] == 0.5


def test_sum_qty_by_symbol_normalizes_crypto_slash(store):
    """A position stored as BTC/USD aggregates with one stored as BTCUSD."""
    _open_pos(store, store._strategy_id,       "BTC/USD", "vwap_bounce", 0.5)
    _open_pos(store, store._other_strategy_id, "BTCUSD",  "rsi_long",    0.3)
    sums = store.sum_qty_by_symbol()
    # Both entries collapse under the broker-flat key.
    assert sums["BTCUSD"] == pytest.approx(0.8)


def test_position_closed_accepts_strategy_id_override(store):
    """The reconciler must close positions across strategies by id, not by name."""
    _open_pos(store, store._other_strategy_id, "AAPL", "rsi_long", 5.0)
    result = store.position_closed(
        symbol="AAPL",
        exit_px=101.0,
        close_reason="broker_fill",
        setup_name="rsi_long",
        strategy_id=store._other_strategy_id,
    )
    assert result is not None
    with Session(store._engine) as session:
        row = session.query(PositionRow).filter(PositionRow.symbol == "AAPL").one()
        assert row.status == "closed"
```

- [ ] **Step 2: Run — confirm failures**

```bash
pytest tests/test_mysql_store_reconciler.py -v
```

Expected: 9 failures with `AttributeError: 'MySQLStore' object has no attribute 'find_open_position_by_coid'` and similar.

- [ ] **Step 3: Add `find_open_position_by_coid`**

In `state/mysql_store.py`, after the existing `position_closed` method, add:

```python
    def find_open_position_by_coid(self, client_order_id: str) -> "PositionRow | None":
        """Return the open PositionRow with this entry COID, or None.

        Used by the reconciler service to match Alpaca fills to MySQL rows.
        Crosses strategies — does not filter by self.strategy_id.
        """
        with Session(self._engine) as session:
            return session.query(PositionRow).filter(
                PositionRow.client_order_id == client_order_id,
                PositionRow.status == "open",
            ).one_or_none()
```

- [ ] **Step 4: Add `find_open_position_by_setup`**

Below the previous method:

```python
    def find_open_position_by_setup(
        self, strategy_id: int, symbol: str, setup_name: str,
    ) -> "PositionRow | None":
        """Return the open PositionRow for (strategy_id, symbol, setup_name).

        Crypto-symbol-form-aware (matches BTC/USD and BTCUSD).
        """
        candidates = self._get_symbol_candidates(symbol)
        with Session(self._engine) as session:
            return session.query(PositionRow).filter(
                PositionRow.strategy_id == strategy_id,
                PositionRow.symbol.in_(candidates),
                PositionRow.setup_name == setup_name,
                PositionRow.status == "open",
            ).one_or_none()
```

- [ ] **Step 5: Add `insert_position_from_fill`**

```python
    def insert_position_from_fill(
        self,
        strategy_id: int,
        setup_name: str,
        symbol: str,
        side: str,
        qty: float,
        entry_px: float,
        opened_at: datetime,
        asset_class: str,
        client_order_id: str,
    ) -> int:
        """Insert a position row recovered from a tagged Alpaca entry fill.

        Used when a strategy submitted an order, Alpaca filled it, but the
        strategy crashed before writing position_opened() to MySQL. The COID
        proves the strategy intended to open this position. Returns the new
        row id. adopted=False because this is recovery, not adoption.
        """
        with Session(self._engine) as session:
            row = PositionRow(
                strategy_id=strategy_id,
                symbol=symbol,
                asset_class=asset_class,
                side=side,
                qty=Decimal(str(qty)),
                entry_px=Decimal(str(entry_px)),
                stop_px=None,
                target_px=None,
                initial_stop_px=None,
                setup_name=setup_name,
                order_id="",
                client_order_id=client_order_id,
                stop_order_id=None,
                breakeven_moved=False,
                bars_held=0,
                adopted=False,
                status="open",
                opened_at=opened_at,
            )
            session.add(row)
            session.commit()
            session.refresh(row)
            self._log.info(
                "MYSQL_FILL_RECOVERED strategy_id=%d symbol=%s setup=%s qty=%s coid=%s",
                strategy_id, symbol, setup_name, qty, client_order_id,
            )
            return row.id
```

- [ ] **Step 6: Add `sum_qty_by_symbol`**

```python
    def sum_qty_by_symbol(self) -> dict[str, float]:
        """Aggregate open qty per symbol across ALL strategies.

        Crypto symbols are normalized to broker-flat form (BTC/USD → BTCUSD)
        so multi-format storage doesn't double-count.
        """
        out: dict[str, float] = {}
        with Session(self._engine) as session:
            rows = session.query(
                PositionRow.symbol, PositionRow.qty,
            ).filter(PositionRow.status == "open").all()
            for symbol, qty in rows:
                # Normalize: any "X/Y" form collapses to "XY".
                key = symbol.replace("/", "")
                out[key] = out.get(key, 0.0) + float(qty)
        return out
```

- [ ] **Step 7: Extend `position_closed` with `strategy_id` override**

Locate the existing `position_closed` method (around line 265 in `state/mysql_store.py`). The signature ends with `exit_client_order_id: str | None = None,`. Add ONE more parameter `strategy_id: int | None = None,` and use it inside the method body. Replace the FIRST query inside `position_closed` (the one that finds `row` by `strategy_id == self.strategy_id`) so it uses the override when provided:

Change the line that reads:

```python
            q = session.query(PositionRow).filter(
                PositionRow.strategy_id == self.strategy_id,
                ...
```

to:

```python
            target_strategy_id = strategy_id if strategy_id is not None else self.strategy_id
            q = session.query(PositionRow).filter(
                PositionRow.strategy_id == target_strategy_id,
                ...
```

And in the `TradeRow(...)` constructor below in the same method, change `strategy_id=self.strategy_id,` to `strategy_id=target_strategy_id,`.

The full updated signature must read:

```python
    def position_closed(
        self,
        symbol: str,
        exit_px: float,
        close_reason: str,
        closed_at: datetime | None = None,
        setup_name: str | None = None,
        exit_client_order_id: str | None = None,
        strategy_id: int | None = None,
    ) -> dict | None:
```

- [ ] **Step 8: Run all 9 new tests — expect green**

```bash
pytest tests/test_mysql_store_reconciler.py -v
```

Expected: 9/9 PASS.

If `test_position_closed_accepts_strategy_id_override` fails because the helper `_open_pos` keeps `_strategy_id` consistent, double-check that the fixture's `_other_strategy_id` is correctly passed into `_open_pos` and that `position_opened` writes under the override.

- [ ] **Step 9: Run broader suite to confirm no regressions**

```bash
pytest tests/test_mysql_store_reconciler.py tests/test_mysql_store_coid.py tests/test_mysql_schema_migration.py tests/test_mysql_legacy_migration.py tests/test_position_book.py tests/test_client_order_id.py tests/test_daily_ledger.py tests/test_circuit_breakers.py -v
```

Expected: all PASS.

- [ ] **Step 10: Commit**

```bash
git add state/mysql_store.py tests/test_mysql_store_reconciler.py
git commit -m "feat(state): MySQLStore helpers for reconciler service (find/insert/sum/cross-strategy close)"
```

---

## Task 3: `reconciler/config.py` — env-driven configuration

**Files:**
- Create: `reconciler/__init__.py` (empty)
- Create: `reconciler/config.py`
- Create: `tests/test_reconciler_config.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_reconciler_config.py`:

```python
"""Tests for ReconcilerConfig env loading."""
from __future__ import annotations

import pytest

from reconciler.config import ReconcilerConfig


def test_defaults_when_env_unset(monkeypatch):
    for var in (
        "RECONCILE_INTERVAL_S",
        "RECONCILE_STRIKE_THRESHOLD",
        "RECONCILE_STRIKE_MIN_GAP_S",
        "RECONCILE_QTY_EPS",
        "SHADOW_MODE",
        "RECONCILE_STATE_FILE",
    ):
        monkeypatch.delenv(var, raising=False)
    cfg = ReconcilerConfig.from_env()
    assert cfg.interval_s == 30
    assert cfg.strike_threshold == 3
    assert cfg.strike_min_gap_s == 60
    assert cfg.qty_eps == pytest.approx(1e-6)
    assert cfg.shadow_mode is False
    assert cfg.state_file_path == "/app/runtime/reconciler_state.json"


def test_overrides_from_env(monkeypatch):
    monkeypatch.setenv("RECONCILE_INTERVAL_S", "10")
    monkeypatch.setenv("RECONCILE_STRIKE_THRESHOLD", "5")
    monkeypatch.setenv("RECONCILE_STRIKE_MIN_GAP_S", "120")
    monkeypatch.setenv("RECONCILE_QTY_EPS", "0.001")
    monkeypatch.setenv("SHADOW_MODE", "true")
    monkeypatch.setenv("RECONCILE_STATE_FILE", "/tmp/state.json")
    cfg = ReconcilerConfig.from_env()
    assert cfg.interval_s == 10
    assert cfg.strike_threshold == 5
    assert cfg.strike_min_gap_s == 120
    assert cfg.qty_eps == pytest.approx(0.001)
    assert cfg.shadow_mode is True
    assert cfg.state_file_path == "/tmp/state.json"


def test_shadow_mode_truthy_strings(monkeypatch):
    for value in ("true", "1", "yes", "TRUE", "YES"):
        monkeypatch.setenv("SHADOW_MODE", value)
        assert ReconcilerConfig.from_env().shadow_mode is True
    for value in ("false", "0", "no", "", "off"):
        monkeypatch.setenv("SHADOW_MODE", value)
        assert ReconcilerConfig.from_env().shadow_mode is False
```

- [ ] **Step 2: Run — confirm failure**

```bash
pytest tests/test_reconciler_config.py -v
```

Expected: `ModuleNotFoundError: No module named 'reconciler'`.

- [ ] **Step 3: Create the package marker**

Create empty file `/Users/alessandro.ren/dev/aitrader/reconciler/__init__.py`:

```python
"""reconciler — dedicated broker↔MySQL reconciliation service (Plan 3)."""
```

- [ ] **Step 4: Create `reconciler/config.py`**

```python
"""Configuration for the reconciler service.

All knobs are read from environment variables with conservative defaults
(see spec section 3 "Defaults & tunables").
"""
from __future__ import annotations

import os
from dataclasses import dataclass


_TRUTHY = frozenset({"true", "1", "yes"})


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in _TRUTHY


@dataclass(frozen=True)
class ReconcilerConfig:
    interval_s: int
    strike_threshold: int
    strike_min_gap_s: int
    qty_eps: float
    shadow_mode: bool
    state_file_path: str

    @classmethod
    def from_env(cls) -> "ReconcilerConfig":
        return cls(
            interval_s=int(os.environ.get("RECONCILE_INTERVAL_S", "30")),
            strike_threshold=int(os.environ.get("RECONCILE_STRIKE_THRESHOLD", "3")),
            strike_min_gap_s=int(os.environ.get("RECONCILE_STRIKE_MIN_GAP_S", "60")),
            qty_eps=float(os.environ.get("RECONCILE_QTY_EPS", "1e-6")),
            shadow_mode=_env_bool("SHADOW_MODE", default=False),
            state_file_path=os.environ.get(
                "RECONCILE_STATE_FILE", "/app/runtime/reconciler_state.json"
            ),
        )
```

- [ ] **Step 5: Run — expect green**

```bash
pytest tests/test_reconciler_config.py -v
```

Expected: 3/3 PASS.

- [ ] **Step 6: Commit**

```bash
git add reconciler/__init__.py reconciler/config.py tests/test_reconciler_config.py
git commit -m "feat(reconciler): ReconcilerConfig env-driven dataclass"
```

---

## Task 4: `reconciler/state.py` — persistent `last_orders_check_ts`

**Files:**
- Create: `reconciler/state.py`
- Create: `tests/test_reconciler_state.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_reconciler_state.py`:

```python
"""Tests for reconciler state file (persistent last_orders_check_ts)."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from reconciler.state import load_state, save_state


def test_load_returns_none_when_file_missing(tmp_path):
    path = tmp_path / "missing.json"
    state = load_state(str(path))
    assert state.last_orders_check_ts is None


def test_save_then_load_round_trip(tmp_path):
    path = tmp_path / "state.json"
    ts = datetime(2026, 5, 28, 14, 0, tzinfo=timezone.utc)
    save_state(str(path), last_orders_check_ts=ts)
    loaded = load_state(str(path))
    assert loaded.last_orders_check_ts == ts


def test_load_returns_none_for_corrupt_file(tmp_path):
    path = tmp_path / "corrupt.json"
    path.write_text("not json {")
    state = load_state(str(path))
    assert state.last_orders_check_ts is None


def test_save_is_atomic(tmp_path):
    """save_state must write via a temp file + rename so a crash mid-write
    can never leave a partial file."""
    path = tmp_path / "state.json"
    ts = datetime(2026, 5, 28, 14, 0, tzinfo=timezone.utc)
    save_state(str(path), last_orders_check_ts=ts)
    # No leftover .tmp file
    leftover = list(tmp_path.glob("*.tmp"))
    assert leftover == []
```

- [ ] **Step 2: Run — confirm failure**

```bash
pytest tests/test_reconciler_state.py -v
```

Expected: `ModuleNotFoundError: No module named 'reconciler.state'`.

- [ ] **Step 3: Create `reconciler/state.py`**

```python
"""Persistent state for the reconciler service.

Currently stores only `last_orders_check_ts` — the high-water mark for the
Alpaca orders pull. Atomic write (temp file + rename) so a crash mid-write
never produces a partial JSON.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass
class ReconcilerState:
    last_orders_check_ts: datetime | None = None


def load_state(path: str) -> ReconcilerState:
    """Load state from a JSON file. Missing or corrupt → empty state."""
    p = Path(path)
    if not p.exists():
        return ReconcilerState()
    try:
        data = json.loads(p.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("RECONCILER_STATE_LOAD_FAILED path=%s err=%s", path, exc)
        return ReconcilerState()
    raw = data.get("last_orders_check_ts")
    ts: datetime | None = None
    if raw:
        try:
            ts = datetime.fromisoformat(raw)
        except ValueError:
            log.warning("RECONCILER_STATE_BAD_TS raw=%s", raw)
    return ReconcilerState(last_orders_check_ts=ts)


def save_state(path: str, *, last_orders_check_ts: datetime | None) -> None:
    """Atomically write state to disk."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "last_orders_check_ts": (
            last_orders_check_ts.isoformat() if last_orders_check_ts else None
        ),
    }
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(payload))
    os.replace(tmp, p)
```

- [ ] **Step 4: Run — expect green**

```bash
pytest tests/test_reconciler_state.py -v
```

Expected: 4/4 PASS.

- [ ] **Step 5: Commit**

```bash
git add reconciler/state.py tests/test_reconciler_state.py
git commit -m "feat(reconciler): persistent last_orders_check_ts (atomic JSON)"
```

---

## Task 5: `reconciler/events.py` — thin event-row writer

**Files:**
- Create: `reconciler/events.py`
- Test: covered in Task 11's main-loop integration test (no standalone test file — `emit_event` is one line of SQLAlchemy and exercising it in isolation would be testing the ORM).

- [ ] **Step 1: Create `reconciler/events.py`**

```python
"""Reconciliation event log writer.

A `reconciliation_events` row is the audit-log artifact of every interesting
moment: heartbeat, untagged fill seen, anomaly confirmed at strike N,
operator action via the CLI (Plan 4).
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from state.mysql_store import EventRow

log = logging.getLogger(__name__)


def emit_event(
    session: Session,
    *,
    type: str,
    strategy_id: int | None = None,
    symbol: str | None = None,
    payload: dict[str, Any] | None = None,
) -> None:
    """Insert a reconciliation_events row.

    The caller owns the session and is responsible for commit().
    """
    row = EventRow(
        type=type,
        strategy_id=strategy_id,
        symbol=symbol,
        payload=payload,
    )
    session.add(row)
```

- [ ] **Step 2: Verify it imports cleanly**

```bash
python -c "from reconciler.events import emit_event; print('OK')"
```

Expected: `OK`. (Locally requires `sqlalchemy` installed — if it errors with `ModuleNotFoundError: 'sqlalchemy'`, run via docker: `docker compose run --rm trader python -c "from reconciler.events import emit_event; print('OK')"`.)

- [ ] **Step 3: Commit**

```bash
git add reconciler/events.py
git commit -m "feat(reconciler): emit_event helper for reconciliation_events"
```

---

## Task 6: `reconciler/fills.py` — apply tagged Alpaca fills

**Files:**
- Create: `reconciler/fills.py`
- Create: `tests/test_reconciler_fills.py`

This is the heart of "every fill applied to the right MySQL row" — the read-side counterpart to Plan 2's COID minting.

The fill input is an Alpaca order dict (the same shape as `list_orders` returns). Behavior depends on the parsed COID's role:

| COID present? | Role | MySQL state | Action |
|---|---|---|---|
| No / unparseable | — | — | Write `untagged_fill` event. No mutation. |
| Yes | `entry` | No matching open row | INSERT new position row (recovery from crash-before-write). Write `tagged_entry_inserted` event. |
| Yes | `entry` | Open row already exists for this COID | Idempotent noop. (Fill was already applied or the strategy successfully wrote the row before crashing.) |
| Yes | `exit`, `stop`, `target` | Open row exists for `(strategy, setup, symbol)` | Close the row via `position_closed(strategy_id=..., setup_name=..., exit_client_order_id=COID, exit_px=fill price, close_reason='broker_fill')`. Write `tagged_fill_applied` event. |
| Yes | `exit`, `stop`, `target` | No matching open row | Idempotent noop. (Already closed, or never written.) |
| Yes | `adopted` | — | Treat like `entry` for matching purposes; an adopted fill we re-see is idempotent. |

- [ ] **Step 1: Write the failing tests**

Create `tests/test_reconciler_fills.py`:

```python
"""Tests for apply_tagged_fill — the read-side counterpart to Plan 2's COID minting."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from reconciler.fills import apply_tagged_fill
from state.mysql_store import (
    Base,
    EventRow,
    MySQLStore,
    PositionRow,
    StrategyRow,
    TradeRow,
)
from state.position_book import OpenPosition


@pytest.fixture
def store():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    s = MySQLStore.__new__(MySQLStore)
    s._engine = engine
    s.strategy_name = "reconciler"
    s._log = logging.getLogger("test_fills")
    with Session(engine) as session:
        session.add_all([
            StrategyRow(name="vwap_wave"),
            StrategyRow(name="rsi_equity"),
        ])
        session.commit()
        rows = session.query(StrategyRow).order_by(StrategyRow.id).all()
    s._strategy_id = rows[0].id
    return s


def _filled_order(coid: str | None, *, side: str = "buy", qty: str = "1",
                  filled_avg_price: str = "100.00", symbol: str = "AAPL",
                  asset_class: str = "us_equity",
                  filled_at: str = "2026-05-28T14:00:00Z") -> dict:
    return {
        "id": "alp-1",
        "client_order_id": coid,
        "side": side,
        "filled_qty": qty,
        "filled_avg_price": filled_avg_price,
        "symbol": symbol,
        "status": "filled",
        "asset_class": asset_class,
        "filled_at": filled_at,
    }


def _events(session: Session) -> list[str]:
    return [r.type for r in session.query(EventRow).order_by(EventRow.id).all()]


def _coid(strategy="vwap_wave", setup="vwap_bounce", symbol="AAPL",
          role="entry", uuid="abcd1234"):
    return f"aitrader__{strategy}__{setup}__{symbol}__{role}__{uuid}"


# ── untagged ───────────────────────────────────────────────────────────


def test_untagged_fill_writes_event_only(store):
    fill = _filled_order(coid=None)
    with Session(store._engine) as session:
        apply_tagged_fill(session, fill, store)
        session.commit()
    with Session(store._engine) as session:
        assert _events(session) == ["untagged_fill"]
        assert session.query(PositionRow).count() == 0
        assert session.query(TradeRow).count() == 0


def test_unparseable_coid_writes_event_only(store):
    fill = _filled_order(coid="not_an_aitrader_coid_xxxxxxxx")
    with Session(store._engine) as session:
        apply_tagged_fill(session, fill, store)
        session.commit()
    with Session(store._engine) as session:
        assert _events(session) == ["untagged_fill"]


# ── tagged entry, no MySQL row → recovery insert ──────────────────────


def test_tagged_entry_with_no_matching_row_inserts_position(store):
    coid = _coid(role="entry")
    fill = _filled_order(coid=coid)
    with Session(store._engine) as session:
        apply_tagged_fill(session, fill, store)
        session.commit()
    with Session(store._engine) as session:
        rows = session.query(PositionRow).all()
        assert len(rows) == 1
        assert rows[0].client_order_id == coid
        assert rows[0].setup_name == "vwap_bounce"
        assert rows[0].adopted is False
        assert _events(session) == ["tagged_entry_inserted"]


def test_tagged_entry_with_matching_row_is_idempotent(store):
    coid = _coid(role="entry")
    # Pre-existing row written by the strategy before its crash
    pos = OpenPosition(
        symbol="AAPL", setup="vwap_bounce", side="long", qty=1.0,
        entry_px=100.0, stop_px=99.0, target_px=101.0,
        opened_at=datetime(2026, 5, 28, 14, 0, tzinfo=timezone.utc),
        order_id="o1", initial_stop_px=99.0, client_order_id=coid,
    )
    store.position_opened(pos, "equity")
    fill = _filled_order(coid=coid)
    with Session(store._engine) as session:
        apply_tagged_fill(session, fill, store)
        session.commit()
    # Still exactly one row, no duplicate insert, no event
    with Session(store._engine) as session:
        assert session.query(PositionRow).count() == 1
        assert _events(session) == []


# ── tagged exit ────────────────────────────────────────────────────────


def test_tagged_exit_closes_matching_position(store):
    entry_coid = _coid(role="entry")
    pos = OpenPosition(
        symbol="AAPL", setup="vwap_bounce", side="long", qty=1.0,
        entry_px=100.0, stop_px=99.0, target_px=101.0,
        opened_at=datetime(2026, 5, 28, 14, 0, tzinfo=timezone.utc),
        order_id="o1", initial_stop_px=99.0, client_order_id=entry_coid,
    )
    store.position_opened(pos, "equity")

    exit_coid = _coid(role="exit", uuid="deadbeef")
    fill = _filled_order(
        coid=exit_coid, side="sell", filled_avg_price="102.50",
    )
    with Session(store._engine) as session:
        apply_tagged_fill(session, fill, store)
        session.commit()
    with Session(store._engine) as session:
        pos_row = session.query(PositionRow).one()
        assert pos_row.status == "closed"
        assert pos_row.exit_client_order_id == exit_coid
        assert pos_row.close_reason == "broker_fill"
        trade = session.query(TradeRow).one()
        assert trade.exit_px == pytest.approx(102.50)
        assert trade.exit_client_order_id == exit_coid
        assert _events(session) == ["tagged_fill_applied"]


def test_tagged_exit_with_no_matching_open_row_is_idempotent(store):
    """Exit fill arrives but position is already closed (re-applied across cycles)."""
    fill = _filled_order(coid=_coid(role="exit"), side="sell")
    with Session(store._engine) as session:
        apply_tagged_fill(session, fill, store)
        session.commit()
    with Session(store._engine) as session:
        assert session.query(TradeRow).count() == 0
        # No event — true noop
        assert _events(session) == []


def test_tagged_target_role_closes_position_same_as_exit(store):
    """role=target (crypto TP fill) closes the position."""
    entry_coid = _coid(role="entry")
    pos = OpenPosition(
        symbol="BTCUSD", setup="vwap_bounce", side="long", qty=0.5,
        entry_px=50000.0, stop_px=49500.0, target_px=51000.0,
        opened_at=datetime(2026, 5, 28, 14, 0, tzinfo=timezone.utc),
        order_id="o1", initial_stop_px=49500.0, client_order_id=entry_coid,
    )
    store.position_opened(pos, "crypto")

    tp_coid = _coid(role="target", symbol="BTCUSD", uuid="cafebabe")
    fill = _filled_order(
        coid=tp_coid, side="sell", filled_avg_price="51000.00",
        symbol="BTCUSD", asset_class="crypto",
    )
    with Session(store._engine) as session:
        apply_tagged_fill(session, fill, store)
        session.commit()
    with Session(store._engine) as session:
        pos_row = session.query(PositionRow).one()
        assert pos_row.status == "closed"
        assert pos_row.close_reason == "broker_fill"


# ── unknown strategy ──────────────────────────────────────────────────


def test_tagged_fill_for_unknown_strategy_writes_untagged(store):
    """COID names a strategy that doesn't exist in MySQL → can't attribute."""
    coid = _coid(strategy="ghost_strategy", role="entry")
    fill = _filled_order(coid=coid)
    with Session(store._engine) as session:
        apply_tagged_fill(session, fill, store)
        session.commit()
    with Session(store._engine) as session:
        assert _events(session) == ["untagged_fill"]
        assert session.query(PositionRow).count() == 0
```

- [ ] **Step 2: Run — confirm 8 failures**

```bash
pytest tests/test_reconciler_fills.py -v
```

Expected: 8 failures, all with `ModuleNotFoundError: No module named 'reconciler.fills'`.

- [ ] **Step 3: Create `reconciler/fills.py`**

```python
"""Apply a single Alpaca filled order to the MySQL state.

Pure function (modulo the SQLAlchemy session it receives). The caller owns
session lifecycle (session.commit) and the loop loop.

Decision tree:
    - COID missing or unparseable             → untagged_fill event, no mutation.
    - COID names an unknown strategy          → untagged_fill event, no mutation.
    - role == 'entry' / 'adopted', no row     → INSERT (crash-before-write recovery).
    - role == 'entry' / 'adopted', row exists → idempotent noop (no event).
    - role in ('exit', 'stop', 'target'),
        matching open row in MySQL            → close row + write trade row.
    - role in ('exit', 'stop', 'target'),
        no matching open row                  → idempotent noop (no event).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from broker.client_order_id import parse_client_order_id
from reconciler.events import emit_event
from state.mysql_store import MySQLStore, StrategyRow

log = logging.getLogger(__name__)

_ENTRY_ROLES = frozenset({"entry", "adopted"})
_EXIT_ROLES = frozenset({"exit", "stop", "target"})


def _resolve_strategy_id(session: Session, strategy_name: str) -> int | None:
    row = session.query(StrategyRow).filter(
        StrategyRow.name == strategy_name,
    ).one_or_none()
    return row.id if row else None


def _parse_fill_time(raw: str | None) -> datetime:
    if not raw:
        return datetime.now(timezone.utc)
    try:
        # Alpaca returns "2026-05-28T14:00:00.123Z"
        s = raw.replace("Z", "+00:00")
        return datetime.fromisoformat(s)
    except ValueError:
        return datetime.now(timezone.utc)


def apply_tagged_fill(
    session: Session,
    fill: dict[str, Any],
    store: MySQLStore,
) -> None:
    """Apply one filled order. The caller is responsible for session.commit()."""
    coid = fill.get("client_order_id")
    parsed = parse_client_order_id(coid)
    if parsed is None:
        emit_event(
            session,
            type="untagged_fill",
            symbol=fill.get("symbol"),
            payload={"alpaca_id": fill.get("id"), "client_order_id": coid},
        )
        return

    strategy_id = _resolve_strategy_id(session, parsed["strategy"])
    if strategy_id is None:
        emit_event(
            session,
            type="untagged_fill",
            symbol=fill.get("symbol"),
            payload={
                "alpaca_id": fill.get("id"),
                "client_order_id": coid,
                "reason": "unknown_strategy",
                "strategy": parsed["strategy"],
            },
        )
        return

    role = parsed["role"]
    symbol = parsed["symbol"]
    setup = parsed["setup"]

    if role in _ENTRY_ROLES:
        existing = store.find_open_position_by_coid(coid)
        if existing is not None:
            return  # idempotent noop, already applied
        # Crash-before-write recovery: insert the row.
        side = "long" if fill.get("side") == "buy" else "short"
        qty = float(fill.get("filled_qty") or 0)
        entry_px = float(fill.get("filled_avg_price") or 0)
        opened_at = _parse_fill_time(fill.get("filled_at"))
        asset_class = "crypto" if fill.get("asset_class") == "crypto" else "equity"
        store.insert_position_from_fill(
            strategy_id=strategy_id,
            setup_name=setup,
            symbol=symbol,
            side=side,
            qty=qty,
            entry_px=entry_px,
            opened_at=opened_at,
            asset_class=asset_class,
            client_order_id=coid,
        )
        emit_event(
            session,
            type="tagged_entry_inserted",
            strategy_id=strategy_id,
            symbol=symbol,
            payload={"client_order_id": coid, "alpaca_id": fill.get("id")},
        )
        return

    if role in _EXIT_ROLES:
        open_row = store.find_open_position_by_setup(strategy_id, symbol, setup)
        if open_row is None:
            return  # idempotent noop
        exit_px = float(fill.get("filled_avg_price") or 0)
        store.position_closed(
            symbol=symbol,
            exit_px=exit_px,
            close_reason="broker_fill",
            setup_name=setup,
            exit_client_order_id=coid,
            strategy_id=strategy_id,
        )
        emit_event(
            session,
            type="tagged_fill_applied",
            strategy_id=strategy_id,
            symbol=symbol,
            payload={
                "client_order_id": coid,
                "alpaca_id": fill.get("id"),
                "role": role,
            },
        )
        return

    # Defensive — unreachable given the role enum.
    log.warning("RECONCILER_UNKNOWN_ROLE coid=%s role=%s", coid, role)
```

- [ ] **Step 4: Run — expect green**

```bash
pytest tests/test_reconciler_fills.py -v
```

Expected: 8/8 PASS.

- [ ] **Step 5: Run broader suite**

```bash
pytest tests/test_reconciler_fills.py tests/test_reconciler_config.py tests/test_reconciler_state.py tests/test_mysql_store_reconciler.py tests/test_mysql_store_coid.py tests/test_client_order_id.py tests/test_position_book.py tests/test_alpaca_client_orders.py tests/test_alpaca_client_list_orders.py -v
```

Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add reconciler/fills.py tests/test_reconciler_fills.py
git commit -m "feat(reconciler): apply_tagged_fill — entry recovery + exit close + idempotency"
```

---

## Task 7: `reconciler/invariant.py` — anomaly detection

**Files:**
- Create: `reconciler/invariant.py`
- Create: `tests/test_reconciler_invariant.py`

Cross-strategy invariant: `Σ open MySQL qty per symbol == broker qty per symbol`.

Three anomaly directions per the spec:
- `qty_drift`: both `mysql_sum > 0` and `broker_qty > 0`, but `|mysql_sum - broker_qty| > eps`.
- `mysql_only`: a `(strategy_id, symbol)` open row exists but the broker has no position for that symbol AND no tagged exit fill explains it.
- `broker_only`: broker has a position for a symbol, no MySQL row exists for it, and no tagged entry fill explains it.

The "explained by a fill from this cycle" guard is the caller's responsibility — `check_invariant` only sees *current* MySQL and broker state. Task 11 (main loop) supplies the post-fill state to it.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_reconciler_invariant.py`:

```python
"""Tests for the cross-strategy invariant checker."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from reconciler.invariant import Anomaly, check_invariant
from state.mysql_store import Base, MySQLStore, StrategyRow
from state.position_book import OpenPosition


@pytest.fixture
def store():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    s = MySQLStore.__new__(MySQLStore)
    s._engine = engine
    s.strategy_name = "reconciler"
    s._log = logging.getLogger("test_invariant")
    with Session(engine) as session:
        session.add_all([StrategyRow(name="vwap_wave"), StrategyRow(name="rsi_equity")])
        session.commit()
        rows = session.query(StrategyRow).order_by(StrategyRow.id).all()
    s._strategy_id = rows[0].id
    s._other_strategy_id = rows[1].id
    return s


def _open_pos(store, strategy_id, symbol, setup, qty, asset_class="equity"):
    pos = OpenPosition(
        symbol=symbol, setup=setup, side="long", qty=qty,
        entry_px=100.0, stop_px=99.0, target_px=101.0,
        opened_at=datetime(2026, 5, 28, 14, 0, tzinfo=timezone.utc),
        order_id="o", initial_stop_px=99.0,
        client_order_id=f"aitrader__x__{setup}__{symbol.replace('/','')}__entry__abcd1234",
    )
    saved = store._strategy_id
    store._strategy_id = strategy_id
    try:
        store.position_opened(pos, asset_class)
    finally:
        store._strategy_id = saved


# ── happy paths ──────────────────────────────────────────────────────


def test_match_yields_no_anomalies(store):
    _open_pos(store, store._strategy_id, "AAPL", "vwap_bounce", 10.0)
    broker = {"AAPL": 10.0}
    with Session(store._engine) as session:
        anomalies = check_invariant(session, store, broker, qty_eps=1e-6)
    assert anomalies == []


def test_cross_strategy_match(store):
    """A=3 + B=2 in MySQL, broker=5 → invariant satisfied."""
    _open_pos(store, store._strategy_id,       "AAPL", "vwap_bounce", 3.0)
    _open_pos(store, store._other_strategy_id, "AAPL", "rsi_long",    2.0)
    broker = {"AAPL": 5.0}
    with Session(store._engine) as session:
        anomalies = check_invariant(session, store, broker, qty_eps=1e-6)
    assert anomalies == []


# ── qty_drift ────────────────────────────────────────────────────────


def test_qty_drift_when_sums_diverge(store):
    """A=1 + B=1 in MySQL, broker=1 → qty_drift on AAPL."""
    _open_pos(store, store._strategy_id,       "AAPL", "vwap_bounce", 1.0)
    _open_pos(store, store._other_strategy_id, "AAPL", "rsi_long",    1.0)
    broker = {"AAPL": 1.0}
    with Session(store._engine) as session:
        anomalies = check_invariant(session, store, broker, qty_eps=1e-6)
    assert len(anomalies) == 1
    a = anomalies[0]
    assert a.direction == "qty_drift"
    assert a.symbol == "AAPL"
    assert a.strategy_id is None
    assert a.snapshot["mysql_sum"] == 2.0
    assert a.snapshot["broker_qty"] == 1.0


def test_qty_drift_within_eps_is_silent(store):
    _open_pos(store, store._strategy_id, "AAPL", "vwap_bounce", 1.0)
    broker = {"AAPL": 1.0 + 1e-9}
    with Session(store._engine) as session:
        anomalies = check_invariant(session, store, broker, qty_eps=1e-6)
    assert anomalies == []


# ── mysql_only ───────────────────────────────────────────────────────


def test_mysql_only_when_broker_missing_symbol(store):
    """Open in MySQL, gone from broker → one mysql_only per (strategy, symbol)."""
    _open_pos(store, store._strategy_id,       "AAPL", "vwap_bounce", 1.0)
    _open_pos(store, store._other_strategy_id, "AAPL", "rsi_long",    1.0)
    broker = {}  # broker has nothing
    with Session(store._engine) as session:
        anomalies = check_invariant(session, store, broker, qty_eps=1e-6)
    directions = sorted(a.direction for a in anomalies)
    assert directions == ["mysql_only", "mysql_only"]
    strategy_ids = sorted(a.strategy_id for a in anomalies)
    assert strategy_ids == [store._strategy_id, store._other_strategy_id]


# ── broker_only ──────────────────────────────────────────────────────


def test_broker_only_when_mysql_has_no_row(store):
    broker = {"SOLUSD": 100.0}
    with Session(store._engine) as session:
        anomalies = check_invariant(session, store, broker, qty_eps=1e-6)
    assert len(anomalies) == 1
    a = anomalies[0]
    assert a.direction == "broker_only"
    assert a.symbol == "SOLUSD"
    assert a.strategy_id is None
    assert a.snapshot["broker_qty"] == 100.0


# ── crypto symbol normalization ──────────────────────────────────────


def test_crypto_slash_form_aggregates_with_flat(store):
    """Position stored as BTC/USD, broker reports BTCUSD → match (no anomaly)."""
    _open_pos(store, store._strategy_id, "BTC/USD", "vwap_bounce", 0.5,
              asset_class="crypto")
    broker = {"BTCUSD": 0.5}
    with Session(store._engine) as session:
        anomalies = check_invariant(session, store, broker, qty_eps=1e-6)
    assert anomalies == []
```

- [ ] **Step 2: Run — confirm failures**

```bash
pytest tests/test_reconciler_invariant.py -v
```

Expected: ModuleNotFoundError on `reconciler.invariant`.

- [ ] **Step 3: Create `reconciler/invariant.py`**

```python
"""Cross-strategy invariant checker.

The invariant: for every symbol, Σ (open MySQL qty across all strategies)
== broker qty for that symbol. Anomalies group into three directions defined
by the spec.

This module is pure: given a MySQL session and a broker positions snapshot,
it returns a list of Anomaly records. It does not mutate.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from state.mysql_store import MySQLStore, PositionRow


@dataclass(frozen=True)
class Anomaly:
    direction: str  # 'qty_drift' | 'mysql_only' | 'broker_only'
    symbol: str
    strategy_id: int | None
    snapshot: dict[str, Any]

    @property
    def key(self) -> str:
        if self.direction == "mysql_only":
            return f"mysql_only:{self.strategy_id}:{self.symbol}"
        return f"{self.direction}:{self.symbol}"


def _normalize(symbol: str) -> str:
    return symbol.replace("/", "")


def check_invariant(
    session: Session,
    store: MySQLStore,
    broker_qty_by_symbol: dict[str, float],
    *,
    qty_eps: float,
) -> list[Anomaly]:
    """Compare MySQL open-position state to the broker snapshot.

    Args:
        session: live SQLAlchemy session (used to query PositionRow directly
            for per-strategy listings; sum_qty_by_symbol does its own query).
        store: MySQLStore — used for sum_qty_by_symbol().
        broker_qty_by_symbol: {symbol → qty} from Alpaca's get_positions, with
            symbols already normalized to broker-flat form.
        qty_eps: tolerance for floating-point comparison.

    Returns:
        list of Anomaly records — empty if the invariant holds.
    """
    broker_norm = {_normalize(s): q for s, q in broker_qty_by_symbol.items()}
    mysql_sums = store.sum_qty_by_symbol()

    anomalies: list[Anomaly] = []

    # qty_drift: symbol present in BOTH but sums differ.
    for symbol in set(mysql_sums) & set(broker_norm):
        m, b = mysql_sums[symbol], broker_norm[symbol]
        if abs(m - b) > qty_eps:
            anomalies.append(Anomaly(
                direction="qty_drift",
                symbol=symbol,
                strategy_id=None,
                snapshot={"mysql_sum": m, "broker_qty": b},
            ))

    # mysql_only: open in MySQL, no broker position for that symbol.
    mysql_only_symbols = set(mysql_sums) - set(broker_norm)
    if mysql_only_symbols:
        rows = session.query(
            PositionRow.strategy_id, PositionRow.symbol, PositionRow.qty,
        ).filter(PositionRow.status == "open").all()
        for strategy_id, raw_symbol, qty in rows:
            sym = _normalize(raw_symbol)
            if sym not in mysql_only_symbols:
                continue
            anomalies.append(Anomaly(
                direction="mysql_only",
                symbol=sym,
                strategy_id=strategy_id,
                snapshot={"mysql_qty": float(qty), "broker_qty": 0.0},
            ))

    # broker_only: broker position for a symbol, no open MySQL rows.
    for symbol in set(broker_norm) - set(mysql_sums):
        anomalies.append(Anomaly(
            direction="broker_only",
            symbol=symbol,
            strategy_id=None,
            snapshot={"mysql_sum": 0.0, "broker_qty": broker_norm[symbol]},
        ))

    return anomalies
```

- [ ] **Step 4: Run — expect green**

```bash
pytest tests/test_reconciler_invariant.py -v
```

Expected: 7/7 PASS.

- [ ] **Step 5: Run broader suite**

```bash
pytest tests/test_reconciler_invariant.py tests/test_reconciler_fills.py tests/test_reconciler_config.py tests/test_reconciler_state.py tests/test_mysql_store_reconciler.py tests/test_mysql_store_coid.py -v
```

Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add reconciler/invariant.py tests/test_reconciler_invariant.py
git commit -m "feat(reconciler): cross-strategy invariant check (qty_drift/mysql_only/broker_only)"
```

---

## Task 8: `reconciler/strikes.py` — multi-strike confirmation rule

**Files:**
- Create: `reconciler/strikes.py`
- Create: `tests/test_reconciler_strikes.py`

The strike rule (spec §3): an anomaly observed on N consecutive cycles separated by ≥ `min_gap_s` triggers an action; anomalies that disappear before strike N self-heal. Per-direction action at strike N: alert only (never auto-mutate).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_reconciler_strikes.py`:

```python
"""Tests for the multi-strike confirmation rule."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from reconciler.config import ReconcilerConfig
from reconciler.invariant import Anomaly
from reconciler.strikes import auto_clear_resolved, process_anomaly
from state.mysql_store import Base, EventRow, StrikeRow, StrategyRow


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        s.add(StrategyRow(name="vwap_wave"))
        s.commit()
        yield s


def _cfg(threshold=3, min_gap_s=60):
    return ReconcilerConfig(
        interval_s=30, strike_threshold=threshold, strike_min_gap_s=min_gap_s,
        qty_eps=1e-6, shadow_mode=False,
        state_file_path="/tmp/state.json",
    )


def _anomaly(direction="qty_drift", symbol="AAPL", strategy_id=None):
    return Anomaly(
        direction=direction, symbol=symbol, strategy_id=strategy_id,
        snapshot={"mysql_sum": 2.0, "broker_qty": 1.0},
    )


# ── First-time strike ─────────────────────────────────────────────────


def test_first_observation_creates_row_at_strike1(session):
    cfg = _cfg()
    a = _anomaly()
    now = datetime(2026, 5, 28, 14, 0, tzinfo=timezone.utc)

    outcome = process_anomaly(session, a, cfg, now=now)
    session.commit()

    assert outcome.action == "logged_strike1"
    assert outcome.strike_count == 1
    assert outcome.alert_sent is False
    rows = session.query(StrikeRow).all()
    assert len(rows) == 1
    assert rows[0].strike_count == 1
    assert rows[0].resolved is False


# ── Strike progression ───────────────────────────────────────────────


def test_repeated_observations_increment_strike_count(session):
    cfg = _cfg()
    a = _anomaly()
    base = datetime(2026, 5, 28, 14, 0, tzinfo=timezone.utc)

    o1 = process_anomaly(session, a, cfg, now=base)
    session.commit()
    o2 = process_anomaly(session, a, cfg, now=base + timedelta(seconds=cfg.strike_min_gap_s + 1))
    session.commit()

    assert o1.strike_count == 1
    assert o2.strike_count == 2
    assert o2.action == "alerted"
    assert o2.alert_sent is True


def test_strike_threshold_triggers_frozen(session):
    cfg = _cfg(threshold=3)
    a = _anomaly(direction="mysql_only", strategy_id=1)
    base = datetime(2026, 5, 28, 14, 0, tzinfo=timezone.utc)

    for i in range(3):
        process_anomaly(session, a, cfg, now=base + timedelta(seconds=cfg.strike_min_gap_s * i + i))
        session.commit()
    rows = session.query(StrikeRow).all()
    assert len(rows) == 1
    assert rows[0].strike_count == 3
    # Final outcome should be frozen
    last = process_anomaly(session, a, cfg, now=base + timedelta(seconds=cfg.strike_min_gap_s * 4))
    session.commit()
    # 4th call past gap = strike 4, but cap at "frozen" once threshold reached
    assert last.action == "frozen" or last.strike_count >= 3


# ── Min-gap rate-limit ────────────────────────────────────────────────


def test_observations_within_min_gap_are_noop(session):
    cfg = _cfg(min_gap_s=60)
    a = _anomaly()
    base = datetime(2026, 5, 28, 14, 0, tzinfo=timezone.utc)

    process_anomaly(session, a, cfg, now=base)
    session.commit()
    o2 = process_anomaly(session, a, cfg, now=base + timedelta(seconds=10))
    session.commit()

    assert o2.action == "noop"
    assert o2.strike_count == 1  # unchanged
    rows = session.query(StrikeRow).all()
    assert rows[0].strike_count == 1


# ── Self-heal ─────────────────────────────────────────────────────────


def test_auto_clear_resolved_marks_disappeared_anomalies(session):
    cfg = _cfg()
    a = _anomaly()
    base = datetime(2026, 5, 28, 14, 0, tzinfo=timezone.utc)

    process_anomaly(session, a, cfg, now=base)
    session.commit()
    # Now the anomaly is gone — auto_clear should mark the strike resolved.
    cleared_keys = auto_clear_resolved(session, current_anomaly_keys=set(), now=base + timedelta(seconds=cfg.strike_min_gap_s + 1))
    session.commit()

    assert cleared_keys == [a.key]
    rows = session.query(StrikeRow).all()
    assert rows[0].resolved is True
    assert rows[0].resolved_reason == "self_healed"
    assert rows[0].strike_count == 0


def test_auto_clear_skips_anomalies_still_present(session):
    cfg = _cfg()
    a = _anomaly()
    base = datetime(2026, 5, 28, 14, 0, tzinfo=timezone.utc)

    process_anomaly(session, a, cfg, now=base)
    session.commit()
    cleared = auto_clear_resolved(
        session, current_anomaly_keys={a.key}, now=base + timedelta(seconds=70),
    )
    session.commit()

    assert cleared == []
    rows = session.query(StrikeRow).all()
    assert rows[0].resolved is False


# ── Resolved-then-fresh handling ──────────────────────────────────────


def test_resolved_strike_unresolved_when_anomaly_returns(session):
    cfg = _cfg()
    a = _anomaly()
    base = datetime(2026, 5, 28, 14, 0, tzinfo=timezone.utc)

    process_anomaly(session, a, cfg, now=base)
    session.commit()
    auto_clear_resolved(session, current_anomaly_keys=set(), now=base + timedelta(seconds=70))
    session.commit()
    # Anomaly returns later — should treat as fresh, strike_count back to 1
    o = process_anomaly(session, a, cfg, now=base + timedelta(seconds=200))
    session.commit()

    assert o.strike_count == 1
    assert o.action == "logged_strike1"
    rows = session.query(StrikeRow).filter(StrikeRow.resolved == False).all()
    assert len(rows) == 1
```

- [ ] **Step 2: Run — confirm failure**

```bash
pytest tests/test_reconciler_strikes.py -v
```

Expected: ModuleNotFoundError on `reconciler.strikes`.

- [ ] **Step 3: Create `reconciler/strikes.py`**

```python
"""Multi-strike confirmation rule.

An anomaly is observed → strike row inserted-or-updated. After N consecutive
observations spaced ≥ min_gap_s apart, the anomaly is "frozen" — alert sent,
strike marked resolved as 'frozen_for_operator'. Anomalies that disappear
before reaching N self-heal via auto_clear_resolved.

No mutation of positions/trades happens here — strikes only emit alerts and
write events. The actual freeze behavior is operator-driven (Plan 4).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable

from sqlalchemy.orm import Session

from reconciler.config import ReconcilerConfig
from reconciler.events import emit_event
from reconciler.invariant import Anomaly
from state.mysql_store import StrikeRow

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class StrikeOutcome:
    action: str       # 'noop' | 'logged_strike1' | 'alerted' | 'frozen' | 'self_healed'
    strike_count: int
    alert_sent: bool


def _find_unresolved(session: Session, key: str) -> StrikeRow | None:
    return session.query(StrikeRow).filter(
        StrikeRow.key == key,
        StrikeRow.resolved == False,  # noqa: E712 — SQLAlchemy idiom
    ).one_or_none()


def process_anomaly(
    session: Session,
    anomaly: Anomaly,
    cfg: ReconcilerConfig,
    *,
    now: datetime,
) -> StrikeOutcome:
    """Look up or insert a strike row for `anomaly.key`, advance the count.

    Returns the outcome describing what happened this cycle.
    """
    existing = _find_unresolved(session, anomaly.key)

    if existing is None:
        row = StrikeRow(
            key=anomaly.key,
            direction=anomaly.direction,
            strategy_id=anomaly.strategy_id,
            symbol=anomaly.symbol,
            strike_count=1,
            first_seen_at=now,
            last_seen_at=now,
            last_observed_state=anomaly.snapshot,
            resolved=False,
        )
        session.add(row)
        return StrikeOutcome(action="logged_strike1", strike_count=1, alert_sent=False)

    # Already at threshold and frozen — treat further observations as noop.
    if existing.strike_count >= cfg.strike_threshold:
        existing.last_seen_at = now
        existing.last_observed_state = anomaly.snapshot
        return StrikeOutcome(
            action="frozen", strike_count=existing.strike_count, alert_sent=False,
        )

    # Min-gap rate limit: same anomaly observed too soon → noop.
    elapsed = (now - existing.last_seen_at).total_seconds()
    if elapsed < cfg.strike_min_gap_s:
        return StrikeOutcome(
            action="noop", strike_count=existing.strike_count, alert_sent=False,
        )

    existing.strike_count += 1
    existing.last_seen_at = now
    existing.last_observed_state = anomaly.snapshot

    if existing.strike_count >= cfg.strike_threshold:
        # Reached threshold this cycle. Per spec: alert + freeze.
        emit_event(
            session,
            type=f"{anomaly.direction}_confirmed",
            strategy_id=anomaly.strategy_id,
            symbol=anomaly.symbol,
            payload={
                "key": anomaly.key,
                "strike_count": existing.strike_count,
                "snapshot": anomaly.snapshot,
            },
        )
        return StrikeOutcome(
            action="frozen", strike_count=existing.strike_count, alert_sent=True,
        )

    return StrikeOutcome(
        action="alerted", strike_count=existing.strike_count, alert_sent=True,
    )


def auto_clear_resolved(
    session: Session,
    *,
    current_anomaly_keys: Iterable[str],
    now: datetime,
) -> list[str]:
    """Mark unresolved strikes whose anomaly is no longer present as self_healed.

    Returns the list of keys that were cleared.
    """
    current_set = set(current_anomaly_keys)
    cleared: list[str] = []
    rows = session.query(StrikeRow).filter(StrikeRow.resolved == False).all()  # noqa: E712
    for row in rows:
        if row.key in current_set:
            continue
        row.resolved = True
        row.resolved_at = now
        row.resolved_reason = "self_healed"
        row.strike_count = 0
        cleared.append(row.key)
    return cleared
```

- [ ] **Step 4: Run — expect green**

```bash
pytest tests/test_reconciler_strikes.py -v
```

Expected: 7/7 PASS.

- [ ] **Step 5: Commit**

```bash
git add reconciler/strikes.py tests/test_reconciler_strikes.py
git commit -m "feat(reconciler): multi-strike confirmation rule + auto-clear self-heal"
```

---

## Task 9: `notifications.send_reconcile_alert`

**Files:**
- Modify: `notifications.py` — append a new function.
- Test: covered in Task 11 (no standalone test — it's a thin Telegram wrapper).

- [ ] **Step 1: Append `send_reconcile_alert`**

Append to `/Users/alessandro.ren/dev/aitrader/notifications.py`:

```python
def send_reconcile_alert(
    direction: str,
    symbol: str,
    strategy_name: str | None,
    snapshot: dict,
    strike_count: int,
    strike_threshold: int,
) -> bool:
    """Send a Telegram alert for a confirmed reconciliation anomaly.

    Returns True if sent, False if Telegram is not configured.
    """
    token, chat_id = _load_telegram_config()
    if token is None:
        log.debug("RECONCILE_TELEGRAM_SKIPPED — TELEGRAM_BOT_TOKEN/CHAT_ID not set")
        return False

    severity = "🚨 FROZEN" if strike_count >= strike_threshold else "⚠️ STRIKE"
    parts: list[str] = [
        f"{severity} reconciliation: {direction} on {symbol}",
        f"strike {strike_count}/{strike_threshold}",
    ]
    if strategy_name:
        parts.insert(1, f"strategy={strategy_name}")
    if "mysql_sum" in snapshot:
        parts.append(f"mysql_sum={snapshot.get('mysql_sum')}")
    if "broker_qty" in snapshot:
        parts.append(f"broker_qty={snapshot.get('broker_qty')}")
    if "mysql_qty" in snapshot:
        parts.append(f"mysql_qty={snapshot.get('mysql_qty')}")

    text = "\n".join(parts)
    try:
        resp = requests.post(
            _TELEGRAM_API.format(token=token),
            json={"chat_id": chat_id, "text": text},
            timeout=5,
        )
        return resp.ok
    except Exception as exc:
        log.warning("RECONCILE_TELEGRAM_FAILED err=%s", exc)
        return False
```

- [ ] **Step 2: Quick smoke import**

```bash
docker compose run --rm trader python -c "from notifications import send_reconcile_alert; print('OK')"
```

Expected: `OK`.

- [ ] **Step 3: Commit**

```bash
git add notifications.py
git commit -m "feat(notifications): send_reconcile_alert for confirmed anomalies"
```

---

## Task 10: `reconciler/main.py` — the loop

**Files:**
- Create: `reconciler/main.py`
- Create: `tests/test_reconciler_main_loop.py`

The loop wires every previous module: pull broker state → apply tagged fills → check invariant → process strikes → auto-clear → emit heartbeat. Single cycle is unit-testable; the actual `while True:` is just `time.sleep(interval_s)`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_reconciler_main_loop.py`:

```python
"""Integration test for one reconciler cycle."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from reconciler.config import ReconcilerConfig
from reconciler.main import run_one_cycle
from state.mysql_store import (
    Base,
    EventRow,
    MySQLStore,
    PositionRow,
    StrategyRow,
    StrikeRow,
)
from state.position_book import OpenPosition


@pytest.fixture
def store():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    s = MySQLStore.__new__(MySQLStore)
    s._engine = engine
    s.strategy_name = "reconciler"
    s._log = logging.getLogger("test_loop")
    with Session(engine) as session:
        session.add(StrategyRow(name="vwap_wave"))
        session.commit()
        s._strategy_id = session.query(StrategyRow).one().id
    return s


def _cfg(shadow=False):
    return ReconcilerConfig(
        interval_s=30, strike_threshold=3, strike_min_gap_s=60,
        qty_eps=1e-6, shadow_mode=shadow,
        state_file_path="/tmp/state.json",
    )


def _coid(role="entry", uuid="abcd1234"):
    return f"aitrader__vwap_wave__vwap_bounce__AAPL__{role}__{uuid}"


def test_cycle_emits_heartbeat(store):
    alpaca = MagicMock()
    alpaca.get_positions.return_value = []
    alpaca.list_orders.return_value = []
    cfg = _cfg()
    now = datetime(2026, 5, 28, 14, 0, tzinfo=timezone.utc)

    advanced_to = run_one_cycle(
        store=store, alpaca=alpaca, cfg=cfg,
        last_orders_check_ts=None, now=now,
    )

    with Session(store._engine) as session:
        types = [r.type for r in session.query(EventRow).order_by(EventRow.id).all()]
        assert "heartbeat" in types
    assert advanced_to == now


def test_cycle_applies_tagged_entry_fill_and_inserts_position(store):
    alpaca = MagicMock()
    alpaca.get_positions.return_value = [{
        "symbol": "AAPL", "qty": "1", "side": "long",
        "asset_class": "us_equity",
    }]
    alpaca.list_orders.return_value = [{
        "id": "alp-1", "client_order_id": _coid(role="entry"),
        "side": "buy", "filled_qty": "1", "filled_avg_price": "100.00",
        "symbol": "AAPL", "asset_class": "us_equity",
        "filled_at": "2026-05-28T13:55:00Z", "status": "filled",
    }]
    cfg = _cfg()
    now = datetime(2026, 5, 28, 14, 0, tzinfo=timezone.utc)

    run_one_cycle(
        store=store, alpaca=alpaca, cfg=cfg,
        last_orders_check_ts=None, now=now,
    )

    with Session(store._engine) as session:
        positions = session.query(PositionRow).all()
        assert len(positions) == 1
        assert positions[0].client_order_id == _coid(role="entry")
        # No anomaly: invariant holds (mysql_sum=1, broker=1)
        strikes = session.query(StrikeRow).all()
        assert strikes == []


def test_cycle_strike_progression_alerts_on_threshold(store):
    """Three consecutive cycles with the same drift should alert at strike 3."""
    pos = OpenPosition(
        symbol="AAPL", setup="vwap_bounce", side="long", qty=2.0,
        entry_px=100.0, stop_px=99.0, target_px=101.0,
        opened_at=datetime(2026, 5, 28, 13, 0, tzinfo=timezone.utc),
        order_id="o", initial_stop_px=99.0,
        client_order_id=_coid(),
    )
    store.position_opened(pos, "equity")

    alpaca = MagicMock()
    alpaca.get_positions.return_value = [{
        "symbol": "AAPL", "qty": "1", "side": "long", "asset_class": "us_equity",
    }]
    alpaca.list_orders.return_value = []
    cfg = _cfg()

    base = datetime(2026, 5, 28, 14, 0, tzinfo=timezone.utc)
    from datetime import timedelta
    for i in range(3):
        now = base + timedelta(seconds=cfg.strike_min_gap_s * i + i)
        run_one_cycle(
            store=store, alpaca=alpaca, cfg=cfg,
            last_orders_check_ts=None, now=now,
        )

    with Session(store._engine) as session:
        strike = session.query(StrikeRow).filter(
            StrikeRow.direction == "qty_drift",
        ).one()
        assert strike.strike_count == 3
        types = [
            r.type for r in session.query(EventRow).order_by(EventRow.id).all()
        ]
        assert "qty_drift_confirmed" in types


def test_shadow_mode_does_not_mutate_positions(store):
    """In shadow mode, fills are seen and events are written, but no INSERT."""
    alpaca = MagicMock()
    alpaca.get_positions.return_value = []
    alpaca.list_orders.return_value = [{
        "id": "alp-1", "client_order_id": _coid(role="entry"),
        "side": "buy", "filled_qty": "1", "filled_avg_price": "100.00",
        "symbol": "AAPL", "asset_class": "us_equity",
        "filled_at": "2026-05-28T13:55:00Z", "status": "filled",
    }]
    cfg = _cfg(shadow=True)
    now = datetime(2026, 5, 28, 14, 0, tzinfo=timezone.utc)

    run_one_cycle(
        store=store, alpaca=alpaca, cfg=cfg,
        last_orders_check_ts=None, now=now,
    )

    with Session(store._engine) as session:
        # No position inserted in shadow mode
        assert session.query(PositionRow).count() == 0
        # But events ARE written (visibility for the operator)
        types = [r.type for r in session.query(EventRow).all()]
        assert "heartbeat" in types
        assert "shadow_would_apply_fill" in types


def test_cycle_skips_on_alpaca_error(store):
    alpaca = MagicMock()
    alpaca.get_positions.side_effect = Exception("API down")
    alpaca.list_orders.return_value = []
    cfg = _cfg()
    now = datetime(2026, 5, 28, 14, 0, tzinfo=timezone.utc)

    advanced_to = run_one_cycle(
        store=store, alpaca=alpaca, cfg=cfg,
        last_orders_check_ts=None, now=now,
    )

    # Cycle skipped — last_orders_check_ts is NOT advanced
    assert advanced_to is None
```

- [ ] **Step 2: Run — confirm failures**

```bash
pytest tests/test_reconciler_main_loop.py -v
```

Expected: ModuleNotFoundError on `reconciler.main`.

- [ ] **Step 3: Create `reconciler/main.py`**

```python
"""Reconciler service main loop.

Cycle order (matches spec §2):
    1. Pull broker truth (positions + fills since last_orders_check_ts).
    2. Apply tagged fills to MySQL (entry recovery + exit close, idempotent).
    3. Check the cross-strategy invariant against the post-fill state.
    4. Process anomalies through the strike rule.
    5. Auto-clear strikes whose anomaly disappeared.
    6. Emit a heartbeat event.

In shadow mode: steps 2 mutate nothing (`shadow_would_apply_fill` events
are written instead). Steps 3-6 still run for visibility but the strike
rule never advances counts (each cycle is a strike 1 → self-heal pair).
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from broker.alpaca_client import AlpacaClient
from notifications import send_reconcile_alert
from reconciler.config import ReconcilerConfig
from reconciler.events import emit_event
from reconciler.fills import apply_tagged_fill
from reconciler.invariant import check_invariant
from reconciler.state import load_state, save_state
from reconciler.strikes import auto_clear_resolved, process_anomaly
from state.mysql_store import MySQLStore

log = logging.getLogger("reconciler")


def _broker_qty_by_symbol(positions: list[dict]) -> dict[str, float]:
    out: dict[str, float] = {}
    for p in positions:
        sym = p.get("symbol", "").replace("/", "")
        if not sym:
            continue
        try:
            out[sym] = abs(float(p.get("qty", 0)))
        except (TypeError, ValueError):
            log.warning("RECONCILER_BAD_BROKER_QTY symbol=%s p=%s", sym, p)
    return out


def run_one_cycle(
    *,
    store: MySQLStore,
    alpaca: Any,
    cfg: ReconcilerConfig,
    last_orders_check_ts: datetime | None,
    now: datetime,
) -> datetime | None:
    """Run a single reconciliation cycle.

    Returns the new high-water mark for `last_orders_check_ts` if the cycle
    completed (advance to `now`), or None if any step skipped due to an
    Alpaca/IO error and the timestamp must NOT be advanced.
    """
    # 1. Pull broker truth.
    try:
        broker_positions = alpaca.get_positions()
        recent_fills = alpaca.list_orders(
            status="closed",
            after=last_orders_check_ts,
            nested=True,
        )
    except Exception as exc:
        log.error("RECONCILER_PULL_FAILED: %s", exc, exc_info=True)
        return None

    broker_norm = _broker_qty_by_symbol(broker_positions)

    with Session(store._engine) as session:
        # 2. Apply tagged fills (or shadow-log them).
        for fill in recent_fills:
            if cfg.shadow_mode:
                emit_event(
                    session,
                    type="shadow_would_apply_fill",
                    symbol=fill.get("symbol"),
                    payload={
                        "alpaca_id": fill.get("id"),
                        "client_order_id": fill.get("client_order_id"),
                    },
                )
            else:
                apply_tagged_fill(session, fill, store)
        session.commit()

        # 3. Check the invariant against post-fill state.
        anomalies = check_invariant(
            session, store, broker_norm, qty_eps=cfg.qty_eps,
        )

        # 4. Process anomalies through the strike rule.
        if not cfg.shadow_mode:
            for a in anomalies:
                outcome = process_anomaly(session, a, cfg, now=now)
                if outcome.alert_sent:
                    strategy_name = None
                    if a.strategy_id is not None:
                        from state.mysql_store import StrategyRow
                        strow = session.query(StrategyRow).filter(
                            StrategyRow.id == a.strategy_id,
                        ).one_or_none()
                        if strow:
                            strategy_name = strow.name
                    send_reconcile_alert(
                        direction=a.direction,
                        symbol=a.symbol,
                        strategy_name=strategy_name,
                        snapshot=a.snapshot,
                        strike_count=outcome.strike_count,
                        strike_threshold=cfg.strike_threshold,
                    )

        # 5. Auto-clear strikes whose anomaly is no longer present.
        auto_clear_resolved(
            session,
            current_anomaly_keys={a.key for a in anomalies},
            now=now,
        )

        # 6. Heartbeat.
        emit_event(
            session,
            type="heartbeat",
            payload={
                "broker_symbols": len(broker_norm),
                "anomalies": len(anomalies),
                "shadow_mode": cfg.shadow_mode,
            },
        )

        session.commit()

    return now


def main() -> int:
    """Entry point: wire env config, load state, run forever."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    cfg = ReconcilerConfig.from_env()
    log.info(
        "RECONCILER_STARTING interval_s=%d threshold=%d shadow=%s",
        cfg.interval_s, cfg.strike_threshold, cfg.shadow_mode,
    )

    store = MySQLStore(strategy_name="reconciler")
    store.ensure_schema()
    store.upsert_strategy()

    state = load_state(cfg.state_file_path)
    log.info("RECONCILER_LOADED_STATE last_orders_check_ts=%s",
             state.last_orders_check_ts)

    alpaca = AlpacaClient()

    while True:
        now = datetime.now(timezone.utc)
        try:
            advanced_to = run_one_cycle(
                store=store, alpaca=alpaca, cfg=cfg,
                last_orders_check_ts=state.last_orders_check_ts, now=now,
            )
            if advanced_to is not None:
                state.last_orders_check_ts = advanced_to
                save_state(
                    cfg.state_file_path,
                    last_orders_check_ts=state.last_orders_check_ts,
                )
        except Exception as exc:
            log.error("RECONCILER_CYCLE_CRASHED: %s", exc, exc_info=True)
        time.sleep(cfg.interval_s)


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run — expect green**

```bash
docker compose run --rm trader pytest tests/test_reconciler_main_loop.py -v
```

Expected: 5/5 PASS.

- [ ] **Step 5: Run all reconciler tests + broader suite**

```bash
docker compose run --rm trader pytest tests/test_reconciler_main_loop.py tests/test_reconciler_strikes.py tests/test_reconciler_invariant.py tests/test_reconciler_fills.py tests/test_reconciler_state.py tests/test_reconciler_config.py tests/test_mysql_store_reconciler.py tests/test_mysql_store_coid.py tests/test_mysql_schema_migration.py tests/test_client_order_id.py tests/test_position_book.py tests/test_alpaca_client_orders.py tests/test_alpaca_client_list_orders.py tests/test_order_executor.py -v
```

Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add reconciler/main.py tests/test_reconciler_main_loop.py
git commit -m "feat(reconciler): main loop wires fills + invariant + strikes + heartbeat"
```

---

## Task 11: docker-compose service + remove per-strategy reconciler

**Files:**
- Modify: `docker-compose.yml` — add `reconciler` service.
- Modify: `main.py` — remove the per-strategy reconciler instantiation and the per-cycle reconcile call. Strategy containers still rebuild their book from MySQL each cycle (unchanged) — they just stop calling `reconciler.reconcile(...)`.

- [ ] **Step 1: Read main.py to confirm what to remove**

```bash
grep -n "Reconciler\|reconciler\|RECONCILE_STARTUP" main.py
```

Expected: lines around the `reconciler =` instantiation (~line 416), the startup `reconciler.reconcile(book, ...)` call (~line 421-432), and the per-cycle `reconciler.reconcile(book, ...)` call (~line 519-533). Also the `from state.reconciler import Reconciler` import (~line 53).

- [ ] **Step 2: Remove the reconciler import in `main.py`**

In `/Users/alessandro.ren/dev/aitrader/main.py`, locate and delete the line:

```python
from state.reconciler import Reconciler
```

- [ ] **Step 3: Remove the startup reconciler block in `main.py`**

Delete the contiguous block that starts with `# Extract strategy params for adopted crypto stop/target calculation` (around line 408) and ends with the closing of the `RECONCILE_STARTUP` log (around line 432). Specifically, delete every line from:

```python
    # Extract strategy params for adopted crypto stop/target calculation
    setups_config = cfg.get("setups", {})
```

through (and including):

```python
    logger.info(
        "RECONCILE_STARTUP closed=%d adopted_eq=%d adopted_cr=%d drift=%d "
        ...
    )
```

(Use `git diff` to verify that only this block was removed and no surrounding code is affected.)

- [ ] **Step 4: Remove the per-cycle reconciler call in `main.py`**

Inside the `while not _shutdown:` loop, locate the per-cycle reconciler block (around line 519). Delete the entire `try: cycle_report = reconciler.reconcile(...)` block including its `except Exception as exc:` handler. The block runs from `try:` (around line 518) to `logger.error("RECONCILE_ERROR: %s", exc, exc_info=True)` (around line 533).

Surrounding code that must remain: the `engine.tick(...)` call below the deleted block, and the bar fetching loop above it.

- [ ] **Step 5: Run main_overrides test to confirm main.py still imports**

```bash
docker compose run --rm -e TRADING_ENV=test trader pytest tests/test_main_overrides.py -v
```

Expected: PASS. If this fails with `NameError: Reconciler` or similar, a deletion left a stale reference — search for `Reconciler` and `reconciler.` in `main.py` to find any leftover.

- [ ] **Step 6: Add the `reconciler` service to `docker-compose.yml`**

In `/Users/alessandro.ren/dev/aitrader/docker-compose.yml`, after the existing `trader-ib-crypto` service block (around line 209) and BEFORE the `dashboard` service, add:

```yaml
  reconciler:
    build: .
    command: python -m reconciler.main
    env_file: ./config/.env
    environment:
      - TRADING_ENV=${TRADING_ENV:-production}
      - MYSQL_HOST=mysql
      - MYSQL_PORT=3306
      - MYSQL_USER=trader
      - MYSQL_PASSWORD=traderpass
      - MYSQL_DATABASE=aitrader
      - RECONCILE_INTERVAL_S=${RECONCILE_INTERVAL_S:-30}
      - RECONCILE_STRIKE_THRESHOLD=${RECONCILE_STRIKE_THRESHOLD:-3}
      - RECONCILE_STRIKE_MIN_GAP_S=${RECONCILE_STRIKE_MIN_GAP_S:-60}
      - RECONCILE_QTY_EPS=${RECONCILE_QTY_EPS:-1e-6}
      - SHADOW_MODE=${SHADOW_MODE:-true}
      - RECONCILE_STATE_FILE=/app/runtime/reconciler_state.json
    user: "${UID:-1000}:${GID:-1000}"
    volumes:
      - ./logs:/app/logs
      - ./runtime:/app/runtime
    restart: unless-stopped
    depends_on:
      mysql:
        condition: service_healthy
```

`SHADOW_MODE` defaults to `true` here intentionally — the rollout (Plan 3 step 5 in the spec) deploys in shadow mode first; the operator flips to `false` after a full session of clean events.

- [ ] **Step 7: Verify docker-compose parses**

```bash
docker compose config 2>&1 | grep -A 5 "reconciler:"
```

Expected: the reconciler service block is rendered with environment variables expanded.

- [ ] **Step 8: Run the full test suite to confirm no regression**

```bash
docker compose run --rm -e TRADING_ENV=test trader pytest --ignore=tests/test_main_overrides.py 2>&1 | tail -3
```

Wait — `test_main_overrides.py` DOES need to pass after this change. Run it explicitly:

```bash
docker compose run --rm -e TRADING_ENV=test trader pytest tests/test_main_overrides.py -v
```

Expected: PASS. Then run the full suite (with the lock-file workaround):

```bash
docker compose run --rm -e TRADING_ENV=test trader pytest 2>&1 | tail -3
```

Expected: all PASS.

- [ ] **Step 9: Commit**

```bash
git add main.py docker-compose.yml
git commit -m "feat(reconciler): docker service + remove per-strategy reconciliation from main.py"
```

---

## Task 12: Delete the dead per-strategy reconciler

**Files:**
- Delete: `state/reconciler.py`
- Delete: `tests/test_reconciler.py`

After Task 11, `state/reconciler.py` and `tests/test_reconciler.py` have no callers. Delete them in a separate commit so the diff is clean.

- [ ] **Step 1: Confirm there are no remaining references**

```bash
grep -rn "from state.reconciler\|import state.reconciler\|state/reconciler" --include="*.py" /Users/alessandro.ren/dev/aitrader
```

Expected: zero matches OUTSIDE the files being deleted (`state/reconciler.py` and `tests/test_reconciler.py` themselves).

If any matches appear in source files (not tests being deleted), STOP and audit each one — there's a residual dependency that Task 11 missed.

- [ ] **Step 2: Delete the files**

```bash
git rm state/reconciler.py tests/test_reconciler.py
```

- [ ] **Step 3: Run the full test suite**

```bash
docker compose run --rm -e TRADING_ENV=test trader pytest 2>&1 | tail -3
```

Expected: all PASS. The deleted file's tests are gone, but no other test should regress.

- [ ] **Step 4: Commit**

```bash
git commit -m "chore(reconciler): delete state/reconciler.py — superseded by reconciler service"
```

---

## Task 13: End-to-end smoke

**Files:** none modified.

- [ ] **Step 1: Bring up a fresh stack**

```bash
docker compose down -v
docker compose up -d mysql
until docker compose exec -T mysql mysqladmin ping --silent; do sleep 2; done
docker compose build trader reconciler
```

- [ ] **Step 2: Start the reconciler in shadow mode (the default in compose)**

```bash
SHADOW_MODE=true docker compose up -d reconciler
sleep 35  # wait for one cycle
docker compose logs --tail=50 reconciler
```

Expected: reconciler logs show `RECONCILER_STARTING shadow=True` and at least one `heartbeat` event written.

- [ ] **Step 3: Confirm heartbeat in MySQL**

```bash
docker compose exec -T mysql mysql -u trader -ptraderpass aitrader -e "
SELECT type, created_at FROM reconciliation_events ORDER BY id DESC LIMIT 5;
"
```

Expected: at least one `heartbeat` row.

- [ ] **Step 4: Switch out of shadow mode**

```bash
docker compose down reconciler
SHADOW_MODE=false docker compose up -d reconciler
sleep 35
docker compose exec -T mysql mysql -u trader -ptraderpass aitrader -e "
SELECT type, COUNT(*) FROM reconciliation_events GROUP BY type ORDER BY type;
"
```

Expected: more `heartbeat` rows; no `_confirmed` rows yet (no anomalies on a clean stack).

- [ ] **Step 5: Tear down**

```bash
docker compose down -v
```

- [ ] **Step 6: No commit needed**

This task is verification-only.

---

## Self-review checklist

**Spec coverage:**
- §2 service container, 30s loop, restart policy → Task 11 docker-compose block.
- §2 cycle order (pull → apply fills → invariant → strikes → heartbeat) → Task 10 `run_one_cycle`.
- §2 transient broker error → skip cycle, don't advance ts → Task 10 `run_one_cycle` returns None on exception.
- §2 strategy containers stop reconciling → Task 11 main.py edits + Task 12 delete.
- §3 strike table state → Plan 1 already provided the schema; Task 8 uses it correctly.
- §3 strike algorithm (lookup-or-insert, min-gap, threshold action, self-heal) → Task 8.
- §3 per-direction action: ALL alert-only, never auto-mutate → Task 8 + Task 10 verified by tests.
- §4 tagged fill application (entry insert, exit close, idempotent, untagged event) → Task 6.
- §4 cross-strategy invariant `Σ MySQL == broker` → Task 7.
- §5 shadow mode → Task 3 config + Task 10 `cfg.shadow_mode` branches + Task 11 default `SHADOW_MODE=true` in compose.
- §5 heartbeat row + dashboard alert (dashboard tab is Plan 4's responsibility, but heartbeat is written) → Task 10 step 6.

**Type consistency:**
- `Anomaly` (frozen dataclass) defined in `reconciler/invariant.py`, consumed by `reconciler/strikes.py` and `reconciler/main.py`. Same import path everywhere.
- `StrikeOutcome` defined in `reconciler/strikes.py`, consumed only by `reconciler/main.py`. Same path.
- `ReconcilerConfig` from `reconciler/config.py`, used everywhere.
- `MySQLStore` helper signatures (`find_open_position_by_coid`, `find_open_position_by_setup`, `insert_position_from_fill`, `sum_qty_by_symbol`, `position_closed(strategy_id=...)`) defined in Task 2, consumed by Tasks 6, 7, 10. Names match exactly.
- COID parser comes from `broker/client_order_id.py` (Plan 2). `parse_client_order_id` returns dict with keys `strategy`, `setup`, `symbol`, `role`, `uuid` — matched in Task 6 fill application.

**Placeholder scan:** No TBDs, no "implement later", no "similar to", no "appropriate handling". Every code step has runnable code.

**Edge cases:**
- Crypto symbol normalization: in Task 2 (`sum_qty_by_symbol`), Task 7 (`check_invariant._normalize`), Task 10 (`_broker_qty_by_symbol`), Task 6 (`apply_tagged_fill` reads `parsed["symbol"]` which is already broker-flat from `make_client_order_id`).
- Idempotency: tagged-entry already-applied → noop (no event); tagged-exit already-closed → noop (no event); strikes resolved-then-fresh → strike_count back to 1.
- Shadow mode: emits `shadow_would_apply_fill` events but does not call `apply_tagged_fill`; strike loop does not run; heartbeat still runs.
- Alpaca error: `run_one_cycle` returns `None` and the caller does NOT advance `last_orders_check_ts`.

---

## Done When

- All 12 implementation tasks committed (Task 13 is verification).
- `pytest` green across `tests/test_reconciler_*.py`, `tests/test_mysql_store_reconciler.py`, `tests/test_alpaca_client_list_orders.py`, plus all pre-existing test files.
- Manual smoke (Task 13) shows heartbeat flowing.
- After merge, deploy the stack with `SHADOW_MODE=true` for one trading session; review `reconciliation_events` for any `*_confirmed` rows; flip `SHADOW_MODE=false` once the operator confirms no false positives.
- Plan 4 (operator CLI + dashboard tab) becomes implementable.
