# Broker↔MySQL Reconciliation v2 — Plan 4: Operator CLI + Dashboard Tab + Heartbeat Alerting

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the operator the tools to act on what the reconciler service produces — list/inspect/resolve frozen strikes via CLI, see them at a glance on the dashboard, and get paged when the reconciler stops emitting heartbeats. After this plan, the reconciliation v2 redesign is complete.

**Architecture:** Three independent surfaces over the same MySQL data. (1) `scripts/reconcile_resolve.py` — operator CLI. Six subcommands (`list`, `show`, `close`, `force-zero`, `adopt`, `extend`, `dismiss`); every action writes a `reconciliation_events` audit row. (2) New `Reconciliation` dashboard tab — read-only Streamlit panel reading the same tables via the existing `ui/data/db.py` engine. (3) Heartbeat staleness watchdog — a small loop inside the reconciler service itself that fires `send_reconcile_alert` when no event has been written in ≥ 5 minutes (configurable). Watching itself is acceptable because the watchdog runs in the same process as the heartbeat — if the process dies, docker `restart: unless-stopped` plus an external watchdog (out of scope) handle that.

**Tech Stack:** Python 3.12, MySQL 8 via SQLAlchemy 2.x ORM (existing `MySQLStore` + `EventRow`/`StrikeRow`), pandas, Streamlit (existing dashboard pattern), pytest, the `notifications.send_reconcile_alert` helper from Plan 3.

**Spec:** `docs/superpowers/specs/2026-05-28-broker-mysql-reconciliation-design.md` §5 — operator CLI subcommands, dashboard surface, heartbeat staleness alert.

**Builds on:** Plans 1–3 (all merged). Schema, COIDs, reconciler service in shadow mode (or live) are all in place.

---

## File Structure

**Create:**

- `scripts/reconcile_resolve.py` — operator CLI. Single file, argparse subcommands, ~350 lines. No tests for the bash-glue argparse — but the helper functions it calls live in `state/mysql_store.py` and ARE tested.
- `state/mysql_store.py` — five new helper methods (the operator CLI's data-access surface). Each ≤ 40 lines, fully unit-tested.
- `ui/data/reconciliation_repo.py` — read-only repo for the dashboard (mirrors `positions_repo.py`/`trades_repo.py` style). ~80 lines.
- `ui/tabs/reconciliation_tab.py` — Streamlit tab. Three panels: heartbeat freshness banner, unresolved strikes table, recent events feed. ~140 lines.
- `tests/test_mysql_store_operator.py` — unit tests for the new MySQLStore helpers. ~200 lines.
- `tests/test_reconciliation_repo.py` — read-side dashboard repo tests. ~120 lines.
- `tests/test_reconcile_resolve_cli.py` — CLI tests via subprocess (or direct function calls if the CLI exposes parseable functions). ~150 lines.
- `tests/test_reconciler_heartbeat_staleness.py` — staleness watchdog tests. ~80 lines.

**Modify:**

- `ui/dashboard.py` — add the `Reconciliation` tab to the tabs list. ~3-line touch.
- `reconciler/main.py` — add a heartbeat-staleness check inside the loop. The check runs after the heartbeat emit; if the *previous* cycle emitted long enough ago to count as stale, fire the alert at startup recovery. Plus the watchdog gets its own period that's independent of `interval_s`. ~30-line addition.
- `reconciler/config.py` — add `heartbeat_stale_after_s` (default 300 = 5 minutes). One line.
- `notifications.py` — add `send_reconcile_heartbeat_stale(last_seen_at, age_seconds)`. ~25 lines.
- `docker-compose.yml` — add `RECONCILE_HEARTBEAT_STALE_AFTER_S=${RECONCILE_HEARTBEAT_STALE_AFTER_S:-300}` to the reconciler env block. One line.

**Untouched (intentionally):**

- The existing dashboard tabs.
- Strategy containers — no changes.
- Plans 1–3 modules.

---

## Operator-CLI surface

Six subcommands, all argparse-based, all dispatch to `MySQLStore` helpers introduced in Task 1. Each mutating command writes a `reconciliation_events` row of `type='operator_action'` for audit trail.

| Command | What it does | MySQL effect |
|---|---|---|
| `list` | Shows all unresolved strike rows | read-only |
| `show <id>` | Full detail for one strike (snapshot, payload, recent matching events) | read-only |
| `close <id> --exit-px <px> --reason <reason>` | For a `mysql_only` strike: close the underlying open `positions` row with the given exit price | `positions` flipped to `closed`, `trades` row inserted, strike resolved |
| `force-zero <id>` | For a `mysql_only` strike: close the position as `pnl=0`, `reason='reconciled_gone'` (the old auto behavior — now requires explicit consent) | `positions` flipped, strike resolved |
| `adopt <id> --strategy X --setup Y` | For a `broker_only` strike: insert a tagged MySQL row for `(X, Y, symbol)` using a synthetic `aitrader__X__Y__SYM__adopted__<uuid>` COID | `positions` row inserted (`adopted=True`), strike resolved |
| `extend <id>` | Reset `strike_count=0`, reopen the case (the operator wants more cycles before acting) | strike row updated, no position mutation |
| `dismiss <id>` | Mark resolved without action ("known external trade", "manual close on broker") | strike resolved with `resolved_reason='operator_dismissed'`, no position mutation |

Every mutating command requires `--note <text>` for the audit trail (or defaults to a stock note like `"operator action via reconcile_resolve.py"`).

---

## Task 1: Five new `MySQLStore` operator-helper methods

**Files:**
- Modify: `state/mysql_store.py` — add 5 new methods near the existing reconciler helpers (around line 870, after `sum_qty_by_symbol`).
- Create: `tests/test_mysql_store_operator.py`

These are the data-access surface the CLI calls into. Putting them on `MySQLStore` (rather than directly in the CLI script) keeps SQL out of the script and makes them testable.

The new methods:
- `list_unresolved_strikes() -> list[StrikeRow]` — read.
- `get_strike_by_id(strike_id) -> StrikeRow | None` — read.
- `resolve_strike(strike_id, reason, operator_note) -> bool` — flip `resolved=True`, set `resolved_at`, `resolved_reason`. Returns False if already resolved.
- `recent_events(limit=50) -> list[EventRow]` — read.
- `events_for_strike(strike_row, limit=20) -> list[EventRow]` — read events whose `symbol`/`strategy_id` match this strike, ordered by `created_at desc`.

Plus one tiny helper added INSIDE `MySQLStore` (not exported on the public surface) used by `reconcile_resolve.py adopt`:
- `insert_adopted_position(strategy_id, setup_name, symbol, side, qty, entry_px, asset_class, opened_at, client_order_id) -> int` — same as `insert_position_from_fill` but with `adopted=True`.

- [ ] **Step 1: Write the failing tests**

Create `/Users/alessandro.ren/dev/aitrader/tests/test_mysql_store_operator.py`:

```python
"""Tests for MySQLStore operator-CLI helpers (Plan 4).

Uses an in-memory SQLite engine to verify the read/resolve/insert paths
the CLI relies on.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from state.mysql_store import (
    Base,
    EventRow,
    MySQLStore,
    PositionRow,
    StrategyRow,
    StrikeRow,
)


@pytest.fixture
def store():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    s = MySQLStore.__new__(MySQLStore)
    s._engine = engine
    s.strategy_name = "operator"
    s._log = logging.getLogger("test_operator")
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


def _strike(store, *, key="qty_drift:AAPL", direction="qty_drift",
            symbol="AAPL", strategy_id=None, count=3, resolved=False):
    base = datetime(2026, 5, 28, 14, 0, tzinfo=timezone.utc)
    with Session(store._engine) as session:
        row = StrikeRow(
            key=key,
            direction=direction,
            strategy_id=strategy_id,
            symbol=symbol,
            strike_count=count,
            first_seen_at=base,
            last_seen_at=base + timedelta(seconds=120),
            last_observed_state={"mysql_sum": 2.0, "broker_qty": 1.0},
            resolved=resolved,
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        return row.id


# ── list_unresolved_strikes ──────────────────────────────────────────


def test_list_unresolved_strikes_returns_only_unresolved(store):
    _strike(store, key="qty_drift:AAPL", resolved=False)
    _strike(store, key="qty_drift:OLD", resolved=True)
    rows = store.list_unresolved_strikes()
    assert len(rows) == 1
    assert rows[0].key == "qty_drift:AAPL"


def test_list_unresolved_strikes_orders_by_last_seen_desc(store):
    base = datetime(2026, 5, 28, 14, 0, tzinfo=timezone.utc)
    with Session(store._engine) as session:
        session.add(StrikeRow(
            key="qty_drift:OLD", direction="qty_drift", symbol="X",
            strike_count=1, first_seen_at=base, last_seen_at=base,
            last_observed_state={}, resolved=False,
        ))
        session.add(StrikeRow(
            key="qty_drift:NEW", direction="qty_drift", symbol="Y",
            strike_count=1, first_seen_at=base, last_seen_at=base + timedelta(seconds=300),
            last_observed_state={}, resolved=False,
        ))
        session.commit()
    rows = store.list_unresolved_strikes()
    assert [r.key for r in rows] == ["qty_drift:NEW", "qty_drift:OLD"]


# ── get_strike_by_id ─────────────────────────────────────────────────


def test_get_strike_by_id_returns_row(store):
    sid = _strike(store)
    row = store.get_strike_by_id(sid)
    assert row is not None
    assert row.id == sid
    assert row.key == "qty_drift:AAPL"


def test_get_strike_by_id_returns_none_for_unknown(store):
    assert store.get_strike_by_id(99999) is None


# ── resolve_strike ───────────────────────────────────────────────────


def test_resolve_strike_marks_resolved_and_writes_event(store):
    sid = _strike(store)
    ok = store.resolve_strike(sid, reason="operator_closed_position",
                              operator_note="manual close on broker")
    assert ok is True
    with Session(store._engine) as session:
        row = session.query(StrikeRow).one()
        assert row.resolved is True
        assert row.resolved_reason == "operator_closed_position"
        assert row.resolved_at is not None
        # Audit-trail event written
        events = session.query(EventRow).all()
        assert any(e.type == "operator_action" for e in events)


def test_resolve_strike_returns_false_when_already_resolved(store):
    sid = _strike(store, resolved=True)
    ok = store.resolve_strike(sid, reason="operator_dismissed",
                              operator_note="manual")
    assert ok is False


def test_resolve_strike_returns_false_when_unknown(store):
    ok = store.resolve_strike(99999, reason="operator_dismissed",
                              operator_note="manual")
    assert ok is False


# ── recent_events ────────────────────────────────────────────────────


def test_recent_events_returns_newest_first(store):
    with Session(store._engine) as session:
        for i in range(3):
            session.add(EventRow(
                type=f"e{i}",
                created_at=datetime(2026, 5, 28, 14, i, tzinfo=timezone.utc),
            ))
        session.commit()
    rows = store.recent_events(limit=10)
    types = [r.type for r in rows]
    assert types == ["e2", "e1", "e0"]


def test_recent_events_respects_limit(store):
    with Session(store._engine) as session:
        for i in range(5):
            session.add(EventRow(type=f"e{i}",
                                 created_at=datetime(2026, 5, 28, 14, i, tzinfo=timezone.utc)))
        session.commit()
    rows = store.recent_events(limit=2)
    assert len(rows) == 2


# ── events_for_strike ────────────────────────────────────────────────


def test_events_for_strike_filters_by_symbol_and_strategy(store):
    sid = _strike(store, key="mysql_only:1:AAPL", direction="mysql_only",
                  symbol="AAPL", strategy_id=store._strategy_id)
    strike = store.get_strike_by_id(sid)

    base = datetime(2026, 5, 28, 14, 0, tzinfo=timezone.utc)
    with Session(store._engine) as session:
        # Match: same symbol + strategy
        session.add(EventRow(type="mysql_only_confirmed",
                             strategy_id=store._strategy_id, symbol="AAPL",
                             created_at=base))
        # No match: same symbol, different strategy
        session.add(EventRow(type="mysql_only_confirmed",
                             strategy_id=store._other_strategy_id, symbol="AAPL",
                             created_at=base))
        # No match: different symbol
        session.add(EventRow(type="heartbeat", symbol="MSFT",
                             created_at=base))
        session.commit()

    events = store.events_for_strike(strike, limit=20)
    types = [(e.type, e.strategy_id, e.symbol) for e in events]
    # Only the matching event survives the filter
    assert (("mysql_only_confirmed", store._strategy_id, "AAPL")
            in types)
    assert all(e.symbol == "AAPL" for e in events)
    assert all(e.strategy_id in (store._strategy_id, None) for e in events)


def test_events_for_strike_no_strategy_id_filter_for_qty_drift(store):
    """qty_drift strikes have strategy_id=None; events match by symbol only."""
    sid = _strike(store, key="qty_drift:AAPL", direction="qty_drift",
                  symbol="AAPL", strategy_id=None)
    strike = store.get_strike_by_id(sid)

    base = datetime(2026, 5, 28, 14, 0, tzinfo=timezone.utc)
    with Session(store._engine) as session:
        session.add(EventRow(type="qty_drift_confirmed",
                             strategy_id=store._strategy_id, symbol="AAPL",
                             created_at=base))
        session.add(EventRow(type="qty_drift_confirmed",
                             strategy_id=store._other_strategy_id, symbol="AAPL",
                             created_at=base))
        session.commit()

    events = store.events_for_strike(strike, limit=20)
    # Both events match because strategy_id is not part of the filter
    assert len(events) == 2


# ── insert_adopted_position ──────────────────────────────────────────


def test_insert_adopted_position_creates_adopted_row(store):
    coid = "aitrader__vwap_wave__adopted__SOLUSD__adopted__abadcafe"
    new_id = store.insert_adopted_position(
        strategy_id=store._strategy_id,
        setup_name="adopted",
        symbol="SOLUSD",
        side="long",
        qty=10.0,
        entry_px=100.0,
        opened_at=datetime(2026, 5, 28, 14, 0, tzinfo=timezone.utc),
        asset_class="crypto",
        client_order_id=coid,
    )
    assert isinstance(new_id, int)
    with Session(store._engine) as session:
        row = session.query(PositionRow).one()
        assert row.adopted is True
        assert row.client_order_id == coid
        assert row.symbol == "SOLUSD"
        assert row.status == "open"
```

- [ ] **Step 2: Run — confirm failures**

```bash
docker compose run --rm trader pytest tests/test_mysql_store_operator.py -v
```

Expected: failures with `AttributeError: 'MySQLStore' object has no attribute 'list_unresolved_strikes'` etc.

- [ ] **Step 3: Add `list_unresolved_strikes`**

In `state/mysql_store.py`, after the existing `sum_qty_by_symbol` method (which the search agent confirmed lives around line 870 from Plan 3 — find by content, not line number), add:

```python
    def list_unresolved_strikes(self) -> list["StrikeRow"]:
        """Return all unresolved reconciliation strikes, newest last_seen first."""
        with Session(self._engine) as session:
            rows = session.query(StrikeRow).filter(
                StrikeRow.resolved == False,  # noqa: E712
            ).order_by(StrikeRow.last_seen_at.desc()).all()
            # Detach so the caller can read scalar columns after session exit
            for r in rows:
                session.expunge(r)
            return rows
```

- [ ] **Step 4: Add `get_strike_by_id`**

Below the previous method:

```python
    def get_strike_by_id(self, strike_id: int) -> "StrikeRow | None":
        """Return the StrikeRow with this id, or None.

        NOTE: returned object is detached — access scalar columns only.
        """
        with Session(self._engine) as session:
            row = session.query(StrikeRow).filter(
                StrikeRow.id == strike_id,
            ).one_or_none()
            if row is not None:
                session.expunge(row)
            return row
```

- [ ] **Step 5: Add `resolve_strike`**

```python
    def resolve_strike(
        self, strike_id: int, *, reason: str, operator_note: str,
    ) -> bool:
        """Flip a strike to resolved with operator-supplied reason.

        Writes an `operator_action` event for audit trail. Returns False
        if the strike doesn't exist or is already resolved.
        """
        from datetime import datetime as _dt, timezone as _tz
        with Session(self._engine) as session:
            row = session.query(StrikeRow).filter(
                StrikeRow.id == strike_id,
            ).one_or_none()
            if row is None or row.resolved:
                return False
            row.resolved = True
            row.resolved_at = _dt.now(_tz.utc)
            row.resolved_reason = reason
            session.add(EventRow(
                type="operator_action",
                strategy_id=row.strategy_id,
                symbol=row.symbol,
                payload={
                    "strike_id": strike_id,
                    "key": row.key,
                    "direction": row.direction,
                    "resolved_reason": reason,
                    "operator_note": operator_note,
                },
            ))
            session.commit()
            return True
```

- [ ] **Step 6: Add `recent_events`**

```python
    def recent_events(self, limit: int = 50) -> list["EventRow"]:
        """Most recent reconciliation_events rows, newest first."""
        with Session(self._engine) as session:
            rows = session.query(EventRow).order_by(
                EventRow.created_at.desc(),
            ).limit(limit).all()
            for r in rows:
                session.expunge(r)
            return rows
```

- [ ] **Step 7: Add `events_for_strike`**

```python
    def events_for_strike(
        self, strike: "StrikeRow", limit: int = 20,
    ) -> list["EventRow"]:
        """Recent events that share this strike's symbol (and strategy_id, if set).

        For qty_drift / broker_only strikes (strategy_id=None), filters by symbol
        only. For mysql_only strikes (strategy_id set), filters by both —
        events with strategy_id=None (e.g. heartbeats) are also included.
        """
        with Session(self._engine) as session:
            q = session.query(EventRow).filter(
                EventRow.symbol == strike.symbol,
            )
            if strike.strategy_id is not None:
                q = q.filter(
                    (EventRow.strategy_id == strike.strategy_id)
                    | (EventRow.strategy_id.is_(None))
                )
            rows = q.order_by(EventRow.created_at.desc()).limit(limit).all()
            for r in rows:
                session.expunge(r)
            return rows
```

- [ ] **Step 8: Add `insert_adopted_position`**

```python
    def insert_adopted_position(
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
        """Insert a position row for an operator-adopted broker_only orphan.

        Same shape as insert_position_from_fill but with adopted=True.
        Used by `scripts/reconcile_resolve.py adopt`.
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
                adopted=True,
                status="open",
                opened_at=opened_at,
            )
            session.add(row)
            session.commit()
            session.refresh(row)
            self._log.info(
                "MYSQL_OPERATOR_ADOPTED strategy_id=%d symbol=%s setup=%s qty=%s coid=%s",
                strategy_id, symbol, setup_name, qty, client_order_id,
            )
            return row.id
```

- [ ] **Step 9: Run — expect green**

```bash
docker compose build trader
docker compose run --rm trader pytest tests/test_mysql_store_operator.py -v
```

Expected: 11/11 PASS.

- [ ] **Step 10: Run broader suite**

```bash
docker compose run --rm trader pytest tests/test_mysql_store_operator.py tests/test_mysql_store_reconciler.py tests/test_mysql_store_coid.py tests/test_mysql_schema_migration.py tests/test_reconciler_main_loop.py tests/test_reconciler_strikes.py tests/test_reconciler_invariant.py tests/test_reconciler_fills.py -v 2>&1 | tail -10
```

Expected: all PASS.

- [ ] **Step 11: Commit**

```bash
git add state/mysql_store.py tests/test_mysql_store_operator.py
git commit -m "feat(state): MySQLStore operator-CLI helpers (list/get/resolve/events/adopt)"
```

---

## Task 2: `scripts/reconcile_resolve.py` — operator CLI

**Files:**
- Create: `scripts/reconcile_resolve.py`
- Create: `tests/test_reconcile_resolve_cli.py`

The CLI is one Python file with argparse subcommands. Each subcommand is a small function that calls into the helpers from Task 1.

The `adopt` subcommand needs to mint a synthetic COID via `broker.client_order_id.make_client_order_id` (Plan 2). The `close` and `force-zero` subcommands need to call `MySQLStore.position_closed`. Everything else just calls `resolve_strike`.

- [ ] **Step 1: Write the failing tests**

Create `/Users/alessandro.ren/dev/aitrader/tests/test_reconcile_resolve_cli.py`:

```python
"""Tests for scripts/reconcile_resolve.py — the operator CLI.

Uses direct function calls (not subprocess) because the CLI subcommands are
exposed as top-level functions in reconcile_resolve. Each test gets a fresh
in-memory SQLite engine via the same fixture pattern as
tests/test_mysql_store_operator.py.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from io import StringIO

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import scripts.reconcile_resolve as cli
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
    s.strategy_name = "operator"
    s._log = logging.getLogger("test_cli")
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


def _add_strike(store, *, direction="qty_drift", symbol="AAPL",
                strategy_id=None, count=3, key=None) -> int:
    base = datetime(2026, 5, 28, 14, 0, tzinfo=timezone.utc)
    with Session(store._engine) as session:
        if key is None:
            key = f"{direction}:{symbol}" if strategy_id is None \
                else f"{direction}:{strategy_id}:{symbol}"
        row = StrikeRow(
            key=key, direction=direction, strategy_id=strategy_id,
            symbol=symbol, strike_count=count,
            first_seen_at=base, last_seen_at=base,
            last_observed_state={"mysql_sum": 2.0, "broker_qty": 1.0},
            resolved=False,
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        return row.id


def _add_open_position(store, strategy_id: int, symbol: str, setup: str,
                       qty: float = 1.0,
                       coid: str = "aitrader__vwap_wave__bounce__AAPL__entry__abcd1234"):
    pos = OpenPosition(
        symbol=symbol, setup=setup, side="long", qty=qty,
        entry_px=100.0, stop_px=99.0, target_px=101.0,
        opened_at=datetime(2026, 5, 28, 13, 0, tzinfo=timezone.utc),
        order_id="o", initial_stop_px=99.0, client_order_id=coid,
    )
    saved = store._strategy_id
    store._strategy_id = strategy_id
    try:
        store.position_opened(pos, "equity")
    finally:
        store._strategy_id = saved


# ── list ─────────────────────────────────────────────────────────────


def test_cmd_list_prints_unresolved_strikes(store, capsys):
    sid = _add_strike(store)
    cli.cmd_list(store)
    out = capsys.readouterr().out
    assert str(sid) in out
    assert "AAPL" in out
    assert "qty_drift" in out


def test_cmd_list_empty(store, capsys):
    cli.cmd_list(store)
    out = capsys.readouterr().out
    assert "no unresolved" in out.lower() or "0 strike" in out.lower()


# ── show ─────────────────────────────────────────────────────────────


def test_cmd_show_prints_full_detail(store, capsys):
    sid = _add_strike(store)
    cli.cmd_show(store, strike_id=sid)
    out = capsys.readouterr().out
    assert str(sid) in out
    assert "qty_drift" in out
    assert "AAPL" in out
    # snapshot rendering
    assert "mysql_sum" in out or "2.0" in out


def test_cmd_show_unknown_id(store, capsys):
    with pytest.raises(SystemExit):
        cli.cmd_show(store, strike_id=99999)


# ── close (mysql_only direction) ─────────────────────────────────────


def test_cmd_close_closes_position_and_resolves_strike(store, capsys):
    _add_open_position(store, store._strategy_id, "AAPL", "vwap_bounce", qty=1.0)
    sid = _add_strike(store, direction="mysql_only", symbol="AAPL",
                      strategy_id=store._strategy_id,
                      key=f"mysql_only:{store._strategy_id}:AAPL")
    cli.cmd_close(store, strike_id=sid, exit_px=100.5,
                  reason="operator_closed_position",
                  setup="vwap_bounce", note="closed by hand on broker")
    with Session(store._engine) as session:
        pos = session.query(PositionRow).one()
        assert pos.status == "closed"
        assert pos.close_reason == "operator_closed_position"
        strike = session.query(StrikeRow).one()
        assert strike.resolved is True
        events = [e.type for e in session.query(EventRow).all()]
        assert "operator_action" in events


def test_cmd_close_rejects_non_mysql_only_strike(store, capsys):
    sid = _add_strike(store, direction="qty_drift")
    with pytest.raises(SystemExit):
        cli.cmd_close(store, strike_id=sid, exit_px=100.5,
                      reason="operator_closed_position",
                      setup="vwap_bounce", note="x")


# ── force-zero ───────────────────────────────────────────────────────


def test_cmd_force_zero_closes_position_with_zero_pnl(store, capsys):
    _add_open_position(store, store._strategy_id, "AAPL", "vwap_bounce")
    sid = _add_strike(store, direction="mysql_only", symbol="AAPL",
                      strategy_id=store._strategy_id,
                      key=f"mysql_only:{store._strategy_id}:AAPL")
    cli.cmd_force_zero(store, strike_id=sid, setup="vwap_bounce",
                       note="known phantom row")
    with Session(store._engine) as session:
        pos = session.query(PositionRow).one()
        assert pos.status == "closed"
        assert pos.close_reason == "reconciled_gone"
        # PnL is 0 (entry_px == exit_px placeholder)
        assert pos.pnl_usd == 0
        strike = session.query(StrikeRow).one()
        assert strike.resolved is True


# ── adopt (broker_only direction) ────────────────────────────────────


def test_cmd_adopt_inserts_position_with_synthetic_coid(store, capsys):
    sid = _add_strike(store, direction="broker_only", symbol="SOLUSD")
    cli.cmd_adopt(store, strike_id=sid, strategy_name="vwap_wave",
                  setup="adopted", side="long", qty=10.0, entry_px=100.0,
                  asset_class="crypto", note="manually opened on broker")
    with Session(store._engine) as session:
        pos = session.query(PositionRow).one()
        assert pos.adopted is True
        assert pos.symbol == "SOLUSD"
        assert pos.client_order_id is not None
        # Synthetic COID has role=adopted
        from broker.client_order_id import parse_client_order_id
        parsed = parse_client_order_id(pos.client_order_id)
        assert parsed is not None
        assert parsed["role"] == "adopted"
        assert parsed["strategy"] == "vwap_wave"
        strike = session.query(StrikeRow).one()
        assert strike.resolved is True


def test_cmd_adopt_rejects_non_broker_only_strike(store, capsys):
    sid = _add_strike(store, direction="qty_drift")
    with pytest.raises(SystemExit):
        cli.cmd_adopt(store, strike_id=sid, strategy_name="vwap_wave",
                      setup="adopted", side="long", qty=1.0, entry_px=100.0,
                      asset_class="equity", note="x")


def test_cmd_adopt_rejects_unknown_strategy(store, capsys):
    sid = _add_strike(store, direction="broker_only", symbol="SOLUSD")
    with pytest.raises(SystemExit):
        cli.cmd_adopt(store, strike_id=sid, strategy_name="ghost_strategy",
                      setup="adopted", side="long", qty=1.0, entry_px=100.0,
                      asset_class="crypto", note="x")


# ── extend ───────────────────────────────────────────────────────────


def test_cmd_extend_resets_strike_count_and_keeps_unresolved(store, capsys):
    sid = _add_strike(store, count=3)
    cli.cmd_extend(store, strike_id=sid, note="want one more cycle")
    with Session(store._engine) as session:
        strike = session.query(StrikeRow).one()
        assert strike.resolved is False  # NOT resolved
        assert strike.strike_count == 0  # reset
        events = [e.type for e in session.query(EventRow).all()]
        assert "operator_action" in events


# ── dismiss ──────────────────────────────────────────────────────────


def test_cmd_dismiss_resolves_with_operator_dismissed(store, capsys):
    sid = _add_strike(store)
    cli.cmd_dismiss(store, strike_id=sid, note="external trade by hand")
    with Session(store._engine) as session:
        strike = session.query(StrikeRow).one()
        assert strike.resolved is True
        assert strike.resolved_reason == "operator_dismissed"
```

- [ ] **Step 2: Run — confirm failures**

```bash
docker compose run --rm trader pytest tests/test_reconcile_resolve_cli.py -v
```

Expected: `ModuleNotFoundError: No module named 'scripts.reconcile_resolve'`.

- [ ] **Step 3: Create `scripts/reconcile_resolve.py`**

```python
#!/usr/bin/env python3
"""Operator CLI for the reconciliation v2 strike-and-event surface.

Subcommands:
  list                                    — show all unresolved strikes
  show <id>                               — full detail for one strike
  close <id> --exit-px <px> --reason <r>  — close a mysql_only position
                  --setup <name> --note <text>
  force-zero <id> --setup <name> --note <text>
                                          — close as pnl=0, reason='reconciled_gone'
  adopt <id> --strategy <s> --setup <s>   — adopt a broker_only orphan as a
                  --side <long|short> --qty <q> --entry-px <p>
                  --asset-class <equity|crypto> --note <text>
  extend <id> --note <text>               — reset strike_count, reopen the case
  dismiss <id> --note <text>              — resolve without action
"""
from __future__ import annotations

import argparse
import json
import sys
import uuid
from datetime import datetime, timezone

from broker.client_order_id import Role, make_client_order_id
from state.mysql_store import MySQLStore, StrategyRow
from sqlalchemy.orm import Session


# ── output helpers ───────────────────────────────────────────────────


def _print_strike_row(s, verbose: bool = False) -> None:
    print(
        f"#{s.id:<5} {s.direction:<12} {s.symbol:<10} "
        f"strike={s.strike_count:<2} last_seen={s.last_seen_at.isoformat()}"
    )
    if verbose:
        print(f"    key       = {s.key}")
        print(f"    strategy  = {s.strategy_id}")
        print(f"    first_seen= {s.first_seen_at.isoformat()}")
        snap = s.last_observed_state
        if isinstance(snap, str):
            try:
                snap = json.loads(snap)
            except json.JSONDecodeError:
                pass
        print(f"    snapshot  = {snap}")


def _resolve_strategy_id_or_exit(store: MySQLStore, name: str) -> int:
    with Session(store._engine) as session:
        row = session.query(StrategyRow).filter(StrategyRow.name == name).one_or_none()
    if row is None:
        print(f"error: unknown strategy {name!r}", file=sys.stderr)
        raise SystemExit(2)
    return row.id


def _get_strike_or_exit(store: MySQLStore, strike_id: int):
    s = store.get_strike_by_id(strike_id)
    if s is None:
        print(f"error: strike #{strike_id} not found", file=sys.stderr)
        raise SystemExit(2)
    return s


def _ensure_direction(strike, expected: str) -> None:
    if strike.direction != expected:
        print(
            f"error: strike #{strike.id} has direction={strike.direction!r}, "
            f"expected {expected!r}",
            file=sys.stderr,
        )
        raise SystemExit(2)


# ── subcommands ──────────────────────────────────────────────────────


def cmd_list(store: MySQLStore) -> None:
    rows = store.list_unresolved_strikes()
    if not rows:
        print("(no unresolved strikes)")
        return
    print(f"{len(rows)} unresolved strike(s):")
    for s in rows:
        _print_strike_row(s, verbose=False)


def cmd_show(store: MySQLStore, strike_id: int) -> None:
    s = _get_strike_or_exit(store, strike_id)
    print(f"strike #{s.id}:")
    _print_strike_row(s, verbose=True)
    events = store.events_for_strike(s, limit=20)
    print(f"  recent events ({len(events)}):")
    for e in events:
        print(f"    {e.created_at.isoformat()}  type={e.type}  payload={e.payload}")


def cmd_close(
    store: MySQLStore, *, strike_id: int, exit_px: float, reason: str,
    setup: str, note: str,
) -> None:
    s = _get_strike_or_exit(store, strike_id)
    _ensure_direction(s, "mysql_only")
    if s.strategy_id is None:
        print(f"error: strike #{strike_id} has no strategy_id", file=sys.stderr)
        raise SystemExit(2)
    result = store.position_closed(
        symbol=s.symbol, exit_px=exit_px, close_reason=reason,
        setup_name=setup, strategy_id=s.strategy_id,
    )
    if result is None:
        print(f"error: no open position for strategy_id={s.strategy_id} "
              f"symbol={s.symbol} setup={setup}", file=sys.stderr)
        raise SystemExit(2)
    store.resolve_strike(strike_id, reason="operator_closed_position",
                         operator_note=note)
    print(f"closed position symbol={s.symbol} setup={setup} "
          f"exit_px={exit_px} pnl={result['pnl_usd']:.2f}")
    print(f"resolved strike #{strike_id}")


def cmd_force_zero(
    store: MySQLStore, *, strike_id: int, setup: str, note: str,
) -> None:
    s = _get_strike_or_exit(store, strike_id)
    _ensure_direction(s, "mysql_only")
    if s.strategy_id is None:
        print(f"error: strike #{strike_id} has no strategy_id", file=sys.stderr)
        raise SystemExit(2)
    # Find the open row to read its entry_px (placeholder for "exit").
    open_row = store.find_open_position_by_setup(s.strategy_id, s.symbol, setup)
    if open_row is None:
        print(f"error: no open position to force-zero", file=sys.stderr)
        raise SystemExit(2)
    result = store.position_closed(
        symbol=s.symbol, exit_px=float(open_row.entry_px),
        close_reason="reconciled_gone", setup_name=setup,
        strategy_id=s.strategy_id,
    )
    if result is None:
        print("error: position_closed returned None", file=sys.stderr)
        raise SystemExit(2)
    store.resolve_strike(strike_id, reason="operator_force_zero",
                         operator_note=note)
    print(f"force-closed position symbol={s.symbol} setup={setup} pnl=0")
    print(f"resolved strike #{strike_id}")


def cmd_adopt(
    store: MySQLStore, *, strike_id: int, strategy_name: str, setup: str,
    side: str, qty: float, entry_px: float, asset_class: str, note: str,
) -> None:
    s = _get_strike_or_exit(store, strike_id)
    _ensure_direction(s, "broker_only")
    strategy_id = _resolve_strategy_id_or_exit(store, strategy_name)
    coid = make_client_order_id(strategy_name, setup, s.symbol, Role.ADOPTED)
    new_pos_id = store.insert_adopted_position(
        strategy_id=strategy_id, setup_name=setup, symbol=s.symbol,
        side=side, qty=qty, entry_px=entry_px, asset_class=asset_class,
        opened_at=datetime.now(timezone.utc), client_order_id=coid,
    )
    store.resolve_strike(strike_id, reason="operator_adopted",
                         operator_note=note)
    print(f"adopted position id={new_pos_id} symbol={s.symbol} "
          f"strategy={strategy_name} setup={setup} coid={coid}")
    print(f"resolved strike #{strike_id}")


def cmd_extend(store: MySQLStore, *, strike_id: int, note: str) -> None:
    s = _get_strike_or_exit(store, strike_id)
    # Reset strike_count to 0 in place — strike remains unresolved.
    from datetime import datetime as _dt, timezone as _tz
    from state.mysql_store import EventRow, StrikeRow
    with Session(store._engine) as session:
        row = session.query(StrikeRow).filter(StrikeRow.id == strike_id).one_or_none()
        if row is None or row.resolved:
            print(f"error: strike #{strike_id} not found or already resolved",
                  file=sys.stderr)
            raise SystemExit(2)
        row.strike_count = 0
        session.add(EventRow(
            type="operator_action",
            strategy_id=row.strategy_id,
            symbol=row.symbol,
            payload={
                "strike_id": strike_id, "key": row.key,
                "operator_action": "extend",
                "operator_note": note,
            },
        ))
        session.commit()
    print(f"extended strike #{strike_id} (count reset to 0, still unresolved)")


def cmd_dismiss(store: MySQLStore, *, strike_id: int, note: str) -> None:
    _get_strike_or_exit(store, strike_id)
    ok = store.resolve_strike(strike_id, reason="operator_dismissed",
                              operator_note=note)
    if not ok:
        print(f"error: strike #{strike_id} could not be dismissed",
              file=sys.stderr)
        raise SystemExit(2)
    print(f"dismissed strike #{strike_id}")


# ── argparse wiring ──────────────────────────────────────────────────


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="reconcile_resolve",
        description="Operator CLI for the reconciliation v2 strike surface.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list")

    sp = sub.add_parser("show")
    sp.add_argument("strike_id", type=int)

    sp = sub.add_parser("close")
    sp.add_argument("strike_id", type=int)
    sp.add_argument("--exit-px", type=float, required=True)
    sp.add_argument("--reason", default="operator_closed_position")
    sp.add_argument("--setup", required=True)
    sp.add_argument("--note", required=True)

    sp = sub.add_parser("force-zero")
    sp.add_argument("strike_id", type=int)
    sp.add_argument("--setup", required=True)
    sp.add_argument("--note", required=True)

    sp = sub.add_parser("adopt")
    sp.add_argument("strike_id", type=int)
    sp.add_argument("--strategy", required=True)
    sp.add_argument("--setup", required=True)
    sp.add_argument("--side", choices=["long", "short"], required=True)
    sp.add_argument("--qty", type=float, required=True)
    sp.add_argument("--entry-px", type=float, required=True)
    sp.add_argument("--asset-class", choices=["equity", "crypto"], required=True)
    sp.add_argument("--note", required=True)

    sp = sub.add_parser("extend")
    sp.add_argument("strike_id", type=int)
    sp.add_argument("--note", required=True)

    sp = sub.add_parser("dismiss")
    sp.add_argument("strike_id", type=int)
    sp.add_argument("--note", required=True)

    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    store = MySQLStore(strategy_name="operator")
    store.ensure_schema()
    store.upsert_strategy()

    if args.cmd == "list":
        cmd_list(store)
    elif args.cmd == "show":
        cmd_show(store, strike_id=args.strike_id)
    elif args.cmd == "close":
        cmd_close(store, strike_id=args.strike_id, exit_px=args.exit_px,
                  reason=args.reason, setup=args.setup, note=args.note)
    elif args.cmd == "force-zero":
        cmd_force_zero(store, strike_id=args.strike_id, setup=args.setup,
                       note=args.note)
    elif args.cmd == "adopt":
        cmd_adopt(store, strike_id=args.strike_id,
                  strategy_name=args.strategy, setup=args.setup,
                  side=args.side, qty=args.qty, entry_px=args.entry_px,
                  asset_class=args.asset_class, note=args.note)
    elif args.cmd == "extend":
        cmd_extend(store, strike_id=args.strike_id, note=args.note)
    elif args.cmd == "dismiss":
        cmd_dismiss(store, strike_id=args.strike_id, note=args.note)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Make sure `scripts/__init__.py` exists**

```bash
ls /Users/alessandro.ren/dev/aitrader/scripts/__init__.py
```

If it doesn't exist (shouldn't happen — Plan 1 confirmed it does), create it as an empty file.

- [ ] **Step 5: Run — expect green**

```bash
docker compose build trader
docker compose run --rm trader pytest tests/test_reconcile_resolve_cli.py -v
```

Expected: 12/12 PASS.

- [ ] **Step 6: Verify CLI argparse manually**

```bash
docker compose run --rm trader python scripts/reconcile_resolve.py --help 2>&1 | head -20
```

Expected: prints the argparse help with all 7 subcommands.

```bash
docker compose run --rm trader python scripts/reconcile_resolve.py list
```

Expected: connects to MySQL (or fails if MySQL isn't up — that's fine for a dry run, just confirms the import path works).

- [ ] **Step 7: Commit**

```bash
git add scripts/reconcile_resolve.py tests/test_reconcile_resolve_cli.py
git commit -m "feat(scripts): reconcile_resolve.py operator CLI (list/show/close/force-zero/adopt/extend/dismiss)"
```

---

## Task 3: `ui/data/reconciliation_repo.py` — read-only dashboard repo

**Files:**
- Create: `ui/data/reconciliation_repo.py`
- Create: `tests/test_reconciliation_repo.py`

Mirror the existing `ui/data/positions_repo.py` style: pure SQLAlchemy reads, returns `pd.DataFrame`s.

The repo exposes:
- `get_unresolved_strikes() -> pd.DataFrame` — id, key, direction, symbol, strategy, strike_count, first_seen_at, last_seen_at, snapshot.
- `get_recent_events(limit=50) -> pd.DataFrame` — id, type, strategy, symbol, payload, created_at.
- `get_heartbeat_freshness() -> dict` — `{"last_seen_at": datetime|None, "age_seconds": float|None}`.

- [ ] **Step 1: Write the failing tests**

Create `/Users/alessandro.ren/dev/aitrader/tests/test_reconciliation_repo.py`:

```python
"""Tests for ui/data/reconciliation_repo.py — read-only dashboard repo."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from state.mysql_store import (
    Base,
    EventRow,
    StrategyRow,
    StrikeRow,
)
from ui.data import reconciliation_repo as repo


@pytest.fixture
def engine(monkeypatch):
    eng = create_engine("sqlite://")
    Base.metadata.create_all(eng)
    with Session(eng) as session:
        session.add_all([
            StrategyRow(name="vwap_wave"),
            StrategyRow(name="rsi_equity"),
        ])
        session.commit()

    # Force the repo to use this engine instead of the real MySQL one.
    from ui.data import db
    monkeypatch.setattr(db, "_engine", eng)
    monkeypatch.setattr(db, "get_engine", lambda: eng)
    return eng


def _add_strike(engine, *, key, direction, symbol, strategy_id=None,
                count=1, last_seen_offset_s=0, resolved=False):
    base = datetime(2026, 5, 28, 14, 0, tzinfo=timezone.utc)
    with Session(engine) as session:
        session.add(StrikeRow(
            key=key, direction=direction, symbol=symbol,
            strategy_id=strategy_id, strike_count=count,
            first_seen_at=base,
            last_seen_at=base + timedelta(seconds=last_seen_offset_s),
            last_observed_state={"mysql_sum": 2.0, "broker_qty": 1.0},
            resolved=resolved,
        ))
        session.commit()


def _add_event(engine, *, type_, symbol=None, strategy_id=None,
               offset_s=0, payload=None):
    base = datetime(2026, 5, 28, 14, 0, tzinfo=timezone.utc)
    with Session(engine) as session:
        session.add(EventRow(
            type=type_, symbol=symbol, strategy_id=strategy_id,
            payload=payload or {},
            created_at=base + timedelta(seconds=offset_s),
        ))
        session.commit()


# ── strikes ──────────────────────────────────────────────────────────


def test_get_unresolved_strikes_returns_dataframe(engine):
    _add_strike(engine, key="qty_drift:AAPL", direction="qty_drift",
                symbol="AAPL")
    _add_strike(engine, key="qty_drift:OLD", direction="qty_drift",
                symbol="OLD", resolved=True)
    df = repo.get_unresolved_strikes()
    assert isinstance(df, pd.DataFrame)
    assert len(df) == 1
    assert df.iloc[0]["symbol"] == "AAPL"
    assert "direction" in df.columns
    assert "strike_count" in df.columns


def test_get_unresolved_strikes_joins_strategy_name(engine):
    with Session(engine) as session:
        strategy_id = session.query(StrategyRow).filter(
            StrategyRow.name == "vwap_wave"
        ).one().id
    _add_strike(engine, key=f"mysql_only:{strategy_id}:AAPL",
                direction="mysql_only", symbol="AAPL",
                strategy_id=strategy_id)
    df = repo.get_unresolved_strikes()
    assert df.iloc[0]["strategy"] == "vwap_wave"


def test_get_unresolved_strikes_qty_drift_has_no_strategy(engine):
    _add_strike(engine, key="qty_drift:AAPL", direction="qty_drift",
                symbol="AAPL", strategy_id=None)
    df = repo.get_unresolved_strikes()
    # NULL strategy → empty / NaN, not crash
    assert df.iloc[0]["strategy"] in (None, "", float("nan")) or \
           pd.isna(df.iloc[0]["strategy"])


# ── events ───────────────────────────────────────────────────────────


def test_get_recent_events_orders_newest_first(engine):
    _add_event(engine, type_="heartbeat", offset_s=60)
    _add_event(engine, type_="heartbeat", offset_s=120)
    _add_event(engine, type_="heartbeat", offset_s=0)
    df = repo.get_recent_events(limit=10)
    assert list(df["type"]) == ["heartbeat", "heartbeat", "heartbeat"]
    # Confirm the timestamps go newest → oldest
    timestamps = list(df["created_at"])
    assert timestamps == sorted(timestamps, reverse=True)


def test_get_recent_events_respects_limit(engine):
    for i in range(5):
        _add_event(engine, type_=f"e{i}", offset_s=i)
    df = repo.get_recent_events(limit=2)
    assert len(df) == 2


# ── heartbeat freshness ──────────────────────────────────────────────


def test_get_heartbeat_freshness_returns_age(engine):
    _add_event(engine, type_="heartbeat", offset_s=0)
    info = repo.get_heartbeat_freshness()
    assert info["last_seen_at"] is not None
    assert info["age_seconds"] is not None
    assert info["age_seconds"] >= 0


def test_get_heartbeat_freshness_no_heartbeat_yet(engine):
    info = repo.get_heartbeat_freshness()
    assert info["last_seen_at"] is None
    assert info["age_seconds"] is None


def test_get_heartbeat_freshness_ignores_other_event_types(engine):
    _add_event(engine, type_="untagged_fill", offset_s=0)
    info = repo.get_heartbeat_freshness()
    assert info["last_seen_at"] is None
```

- [ ] **Step 2: Run — confirm failures**

```bash
docker compose run --rm trader pytest tests/test_reconciliation_repo.py -v
```

Expected: ModuleNotFoundError on `ui.data.reconciliation_repo`.

- [ ] **Step 3: Create `ui/data/reconciliation_repo.py`**

```python
"""Read-only repo for the reconciliation dashboard tab.

Mirrors the style of ui/data/positions_repo.py: SQL via the shared engine,
pandas DataFrames out.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
from sqlalchemy import text

from ui.data.db import get_engine


def get_unresolved_strikes() -> pd.DataFrame:
    """All unresolved reconciliation_strikes joined to strategy name.

    Columns: id, key, direction, symbol, strategy (str|None), strike_count,
             first_seen_at, last_seen_at, last_observed_state.
    Ordered: most recently seen first.
    """
    eng = get_engine()
    with eng.connect() as conn:
        df = pd.read_sql(
            text("""
                SELECT r.id, r.`key`, r.direction, r.symbol,
                       s.name AS strategy,
                       r.strike_count, r.first_seen_at, r.last_seen_at,
                       r.last_observed_state
                FROM reconciliation_strikes r
                LEFT JOIN strategies s ON s.id = r.strategy_id
                WHERE r.resolved = 0
                ORDER BY r.last_seen_at DESC
            """),
            conn,
        )
    return df


def get_recent_events(limit: int = 50) -> pd.DataFrame:
    """Most recent reconciliation_events.

    Columns: id, type, strategy (str|None), symbol, payload, created_at.
    Ordered: newest first.
    """
    eng = get_engine()
    with eng.connect() as conn:
        df = pd.read_sql(
            text("""
                SELECT e.id, e.type, s.name AS strategy, e.symbol,
                       e.payload, e.created_at
                FROM reconciliation_events e
                LEFT JOIN strategies s ON s.id = e.strategy_id
                ORDER BY e.created_at DESC
                LIMIT :limit
            """),
            conn,
            params={"limit": int(limit)},
        )
    return df


def get_heartbeat_freshness() -> dict:
    """Last `heartbeat` event timestamp and its age in seconds.

    Returns:
        {"last_seen_at": datetime | None, "age_seconds": float | None}
    """
    eng = get_engine()
    with eng.connect() as conn:
        result = conn.execute(text("""
            SELECT MAX(created_at) FROM reconciliation_events
            WHERE type = 'heartbeat'
        """)).first()
    last_seen = result[0] if result else None
    if last_seen is None:
        return {"last_seen_at": None, "age_seconds": None}
    if last_seen.tzinfo is None:
        last_seen = last_seen.replace(tzinfo=timezone.utc)
    age = (datetime.now(timezone.utc) - last_seen).total_seconds()
    return {"last_seen_at": last_seen, "age_seconds": age}
```

- [ ] **Step 4: Run — expect green**

```bash
docker compose build trader
docker compose run --rm trader pytest tests/test_reconciliation_repo.py -v
```

Expected: 8/8 PASS.

- [ ] **Step 5: Commit**

```bash
git add ui/data/reconciliation_repo.py tests/test_reconciliation_repo.py
git commit -m "feat(ui): reconciliation_repo for dashboard reads"
```

---

## Task 4: `ui/tabs/reconciliation_tab.py` — Streamlit panel

**Files:**
- Create: `ui/tabs/reconciliation_tab.py`
- Modify: `ui/dashboard.py` — add the new tab.

Three panels in order:

1. **Heartbeat freshness banner** — green if ≤ 60s, yellow ≤ 5min, red beyond. Shows `last_seen_at` ISO and the age.
2. **Unresolved strikes table** — `id, direction, symbol, strategy, strike_count, last_seen_at, snapshot summary`. With a "How to resolve" expander showing the relevant `reconcile_resolve.py` command for each strike's direction.
3. **Recent events feed** — last 50 events. Filterable by type via a sidebar widget. Ordered newest first.

Read-only — no actions taken from the UI. The CLI is the canonical resolution path (auditability + safety).

- [ ] **Step 1: Create the tab file**

Create `/Users/alessandro.ren/dev/aitrader/ui/tabs/reconciliation_tab.py`:

```python
"""Reconciliation tab — read-only view of strikes, events, and heartbeat.

All resolution actions go through scripts/reconcile_resolve.py for
auditability. This tab is for visibility only.
"""
from __future__ import annotations

import json

import streamlit as st

from ui.data import reconciliation_repo as repo


def _heartbeat_color(age_s: float | None) -> str:
    if age_s is None:
        return "gray"
    if age_s <= 60:
        return "green"
    if age_s <= 300:
        return "orange"
    return "red"


def _heartbeat_label(age_s: float | None) -> str:
    if age_s is None:
        return "no heartbeat yet"
    if age_s < 60:
        return f"{int(age_s)}s ago"
    minutes = int(age_s // 60)
    return f"{minutes}m ago"


def _resolve_hint(direction: str) -> str:
    if direction == "mysql_only":
        return (
            "scripts/reconcile_resolve.py close <id> "
            "--exit-px <px> --setup <name> --note <text>\n"
            "  or: force-zero <id> --setup <name> --note <text>"
        )
    if direction == "broker_only":
        return (
            "scripts/reconcile_resolve.py adopt <id> "
            "--strategy <name> --setup <name> --side <long|short> "
            "--qty <q> --entry-px <p> --asset-class <equity|crypto> --note <text>"
        )
    return (
        "scripts/reconcile_resolve.py dismiss <id> --note <text>\n"
        "  (qty_drift can only be operator-resolved manually — "
        "investigate the drift first)"
    )


def render() -> None:
    st.header("Reconciliation")

    # ── Heartbeat banner ──────────────────────────────────────────────
    hb = repo.get_heartbeat_freshness()
    color = _heartbeat_color(hb["age_seconds"])
    label = _heartbeat_label(hb["age_seconds"])
    last_at = hb["last_seen_at"].isoformat() if hb["last_seen_at"] else "—"
    st.markdown(
        f"""<div style="padding: 8px 12px; border-radius: 4px;
            background-color: {color}; color: white; font-weight: 600;">
        Reconciler heartbeat: {label} (last_seen_at={last_at})
        </div>""",
        unsafe_allow_html=True,
    )

    # ── Unresolved strikes ────────────────────────────────────────────
    st.subheader("Unresolved strikes")
    strikes = repo.get_unresolved_strikes()
    if strikes.empty:
        st.success("No unresolved strikes.")
    else:
        st.dataframe(
            strikes[[
                "id", "direction", "symbol", "strategy",
                "strike_count", "last_seen_at", "last_observed_state",
            ]],
            use_container_width=True,
        )
        with st.expander("How to resolve a strike"):
            st.markdown(
                "Every action goes through the operator CLI for audit-trail "
                "reasons. Connect to the trader container:"
            )
            st.code(
                "docker compose exec trader python scripts/reconcile_resolve.py list",
                language="bash",
            )
            st.markdown("**Per-direction commands:**")
            for direction in ("mysql_only", "broker_only", "qty_drift"):
                st.markdown(f"**`{direction}`:**")
                st.code(_resolve_hint(direction), language="bash")

    # ── Recent events ─────────────────────────────────────────────────
    st.subheader("Recent events")
    events = repo.get_recent_events(limit=100)
    if events.empty:
        st.info("No events yet.")
        return
    type_options = ["(all)"] + sorted(events["type"].dropna().unique().tolist())
    selected_type = st.selectbox(
        "Filter by type", type_options, key="reconcile_event_type",
    )
    df = events if selected_type == "(all)" else events[events["type"] == selected_type]
    st.dataframe(
        df[["created_at", "type", "strategy", "symbol", "payload"]],
        use_container_width=True,
    )
```

- [ ] **Step 2: Wire the new tab into `ui/dashboard.py`**

In `/Users/alessandro.ren/dev/aitrader/ui/dashboard.py`, locate the imports section. Add a new import alongside the existing tab imports:

```python
from ui.tabs import config_tab, live_tab, reconciliation_tab, strategies_tab
```

(That replaces the existing `from ui.tabs import config_tab, live_tab, strategies_tab` line.)

Update the tabs declaration. Find:

```python
strategies_t, live_t, config_t, logs_t, wfo_t = st.tabs([
    "Strategies", "Live Trading", "Configuration", "Logs", "WFO",
])
```

Replace with:

```python
strategies_t, live_t, recon_t, config_t, logs_t, wfo_t = st.tabs([
    "Strategies", "Live Trading", "Reconciliation", "Configuration", "Logs", "WFO",
])
```

Add a render block. Find the existing `with config_t:` block. ABOVE it, insert:

```python
with recon_t:
    _safe_render("reconciliation", reconciliation_tab.render)
```

- [ ] **Step 3: Verify dashboard module imports cleanly**

```bash
docker compose build trader
docker compose run --rm -e TRADING_ENV=test trader python -c "import ui.dashboard; print('OK')"
```

Expected: `OK` (no `streamlit run`, just confirm the module imports without error).

If the import fails: re-read the modified `ui/dashboard.py` and ensure the `reconciliation_tab` import is correct and the tab block is in the right place.

- [ ] **Step 4: Smoke the dashboard against a live MySQL container**

```bash
docker compose down -v
docker compose up -d mysql
until docker compose exec -T mysql mysqladmin ping --silent; do sleep 2; done
docker compose up -d dashboard
sleep 3
docker compose logs --tail=20 dashboard
```

Expected: dashboard logs show `You can now view your Streamlit app...`. The Reconciliation tab won't have any data (no reconciler running) — that's fine. We're verifying the import + tab wiring.

- [ ] **Step 5: Tear down**

```bash
docker compose down -v
```

- [ ] **Step 6: Commit**

```bash
git add ui/dashboard.py ui/tabs/reconciliation_tab.py
git commit -m "feat(ui): Reconciliation dashboard tab (heartbeat + strikes + events)"
```

---

## Task 5: Heartbeat staleness detection inside the reconciler

**Files:**
- Modify: `notifications.py` — add `send_reconcile_heartbeat_stale`.
- Modify: `reconciler/config.py` — add `heartbeat_stale_after_s` field + env load.
- Modify: `reconciler/main.py` — emit a stale alert at startup if the previous heartbeat is older than `heartbeat_stale_after_s`. (The watchdog runs once per cycle; if the *prior* cycle didn't fire, the next cycle's first action checks freshness.)
- Create: `tests/test_reconciler_heartbeat_staleness.py` — tests for both the alert helper and the staleness check.
- Modify: `docker-compose.yml` — add `RECONCILE_HEARTBEAT_STALE_AFTER_S` env var.
- Modify: `tests/test_reconciler_config.py` — extend the existing tests to cover the new field.

The staleness check is deliberately simple: at the START of each `run_one_cycle`, if `last_orders_check_ts is not None` AND `(now - last_orders_check_ts) > heartbeat_stale_after_s`, emit one alert (plus an event). This catches the "reconciler restarted after a long gap" case. The container's `restart: unless-stopped` policy plus an external watchdog (out of scope) handle "process is dead".

- [ ] **Step 1: Add `send_reconcile_heartbeat_stale` to `notifications.py`**

Append to `/Users/alessandro.ren/dev/aitrader/notifications.py`:

```python
def send_reconcile_heartbeat_stale(
    last_seen_at: "datetime | None",
    age_seconds: float,
    stale_threshold_s: int,
) -> bool:
    """Telegram alert: reconciler heartbeat older than the staleness threshold.

    Returns True if sent, False if Telegram is not configured.
    """
    token, chat_id = _load_telegram_config()
    if token is None:
        log.debug("RECONCILE_TELEGRAM_SKIPPED — TELEGRAM_BOT_TOKEN/CHAT_ID not set")
        return False

    minutes = int(age_seconds // 60)
    last_str = last_seen_at.isoformat() if last_seen_at is not None else "never"
    text = (
        f"🚨 RECONCILER HEARTBEAT STALE\n"
        f"age={minutes}m ({int(age_seconds)}s)\n"
        f"last_seen_at={last_str}\n"
        f"threshold={stale_threshold_s}s"
    )
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

The function uses `from datetime import datetime` — confirm that's already imported at the top of `notifications.py`. (Check `head -20 notifications.py` — `requests` is there, `datetime` may not be. If not present, the type annotation `"datetime | None"` works thanks to `from __future__ import annotations` — confirm that's at the top of the file. If neither is present, add `from datetime import datetime` to imports.)

- [ ] **Step 2: Add `heartbeat_stale_after_s` to `ReconcilerConfig`**

In `/Users/alessandro.ren/dev/aitrader/reconciler/config.py`:

Add the field to the dataclass (after `state_file_path`):

```python
    heartbeat_stale_after_s: int
```

And to `from_env`:

```python
            heartbeat_stale_after_s=int(os.environ.get(
                "RECONCILE_HEARTBEAT_STALE_AFTER_S", "300"
            )),
```

The full `from_env` should now look like:

```python
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
            heartbeat_stale_after_s=int(os.environ.get(
                "RECONCILE_HEARTBEAT_STALE_AFTER_S", "300"
            )),
        )
```

- [ ] **Step 3: Update `tests/test_reconciler_config.py`**

The existing tests must continue to pass. Find each test that constructs a `ReconcilerConfig` directly (NOT via `from_env`) — there shouldn't be any, but search:

```bash
grep -n "ReconcilerConfig(" tests/test_reconciler_config.py
```

Expected: zero matches — the tests all use `from_env`. If any direct constructions exist, add `heartbeat_stale_after_s=300` to them.

Update the existing tests. In `test_defaults_when_env_unset`, add to the env unsetting block:

```python
        "RECONCILE_HEARTBEAT_STALE_AFTER_S",
```

And add the assertion:

```python
    assert cfg.heartbeat_stale_after_s == 300
```

In `test_overrides_from_env`, add:

```python
    monkeypatch.setenv("RECONCILE_HEARTBEAT_STALE_AFTER_S", "60")
```

And:

```python
    assert cfg.heartbeat_stale_after_s == 60
```

- [ ] **Step 4: Write the failing staleness-detection test**

Create `/Users/alessandro.ren/dev/aitrader/tests/test_reconciler_heartbeat_staleness.py`:

```python
"""Tests for the heartbeat-staleness detection inside run_one_cycle."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
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
    StrategyRow,
)


@pytest.fixture
def store():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    s = MySQLStore.__new__(MySQLStore)
    s._engine = engine
    s.strategy_name = "reconciler"
    s._log = logging.getLogger("test_staleness")
    with Session(engine) as session:
        session.add(StrategyRow(name="vwap_wave"))
        session.commit()
        s._strategy_id = session.query(StrategyRow).one().id
    return s


def _cfg(stale_after_s=300):
    return ReconcilerConfig(
        interval_s=30, strike_threshold=3, strike_min_gap_s=60,
        qty_eps=1e-6, shadow_mode=False,
        state_file_path="/tmp/state.json",
        heartbeat_stale_after_s=stale_after_s,
    )


def test_no_alert_when_no_previous_heartbeat(store):
    """First-ever cycle has no prior heartbeat — no stale alert."""
    alpaca = MagicMock()
    alpaca.get_positions.return_value = []
    alpaca.list_orders.return_value = []
    cfg = _cfg()
    now = datetime(2026, 5, 28, 14, 0, tzinfo=timezone.utc)

    run_one_cycle(store=store, alpaca=alpaca, cfg=cfg,
                  last_orders_check_ts=None, now=now)

    with Session(store._engine) as session:
        types = [e.type for e in session.query(EventRow).all()]
        assert "heartbeat" in types
        assert "reconciler_heartbeat_stale" not in types


def test_no_alert_when_recent_heartbeat(store):
    """Last heartbeat was within the threshold → no alert."""
    base = datetime(2026, 5, 28, 14, 0, tzinfo=timezone.utc)
    # Pre-existing heartbeat 60s ago
    with Session(store._engine) as session:
        session.add(EventRow(
            type="heartbeat",
            created_at=base - timedelta(seconds=60),
        ))
        session.commit()

    alpaca = MagicMock()
    alpaca.get_positions.return_value = []
    alpaca.list_orders.return_value = []
    cfg = _cfg(stale_after_s=300)

    run_one_cycle(store=store, alpaca=alpaca, cfg=cfg,
                  last_orders_check_ts=None, now=base)

    with Session(store._engine) as session:
        types = [e.type for e in session.query(EventRow).all()]
        assert "reconciler_heartbeat_stale" not in types


def test_alert_when_heartbeat_older_than_threshold(store):
    """Last heartbeat older than threshold → emit stale event."""
    base = datetime(2026, 5, 28, 14, 0, tzinfo=timezone.utc)
    # Pre-existing heartbeat 600s ago (threshold = 300s)
    with Session(store._engine) as session:
        session.add(EventRow(
            type="heartbeat",
            created_at=base - timedelta(seconds=600),
        ))
        session.commit()

    alpaca = MagicMock()
    alpaca.get_positions.return_value = []
    alpaca.list_orders.return_value = []
    cfg = _cfg(stale_after_s=300)

    run_one_cycle(store=store, alpaca=alpaca, cfg=cfg,
                  last_orders_check_ts=None, now=base)

    with Session(store._engine) as session:
        events = session.query(EventRow).order_by(EventRow.id).all()
        types = [e.type for e in events]
        assert "reconciler_heartbeat_stale" in types
        assert "heartbeat" in types
        # The stale event must be written BEFORE the new heartbeat
        stale_id = next(e.id for e in events if e.type == "reconciler_heartbeat_stale")
        new_hb_id = max(e.id for e in events if e.type == "heartbeat")
        assert stale_id < new_hb_id
```

- [ ] **Step 5: Run — confirm the new tests fail**

```bash
docker compose build trader
docker compose run --rm trader pytest tests/test_reconciler_heartbeat_staleness.py -v
```

Expected: 3 failures (`reconciler_heartbeat_stale not in types`).

- [ ] **Step 6: Update `reconciler/main.py` to emit the stale check**

Open `/Users/alessandro.ren/dev/aitrader/reconciler/main.py`. Locate the `run_one_cycle` function. AFTER the broker pull but BEFORE the `with Session(...)` block, add a staleness check.

The simplest place to insert is INSIDE the existing `with Session(store._engine) as session:` block, BEFORE the fill-application loop. Put it right after the comment `# 2. Apply tagged fills (or shadow-log them).` — actually before that comment, after `with Session(store._engine) as session:`.

Concretely, the function body becomes (only the new block is the added code; everything else stays):

```python
    with Session(store._engine) as session:
        # 1.5. Heartbeat staleness check (before fills, so the alert is the
        # first artifact written this cycle).
        last_hb = session.query(EventRow.created_at).filter(
            EventRow.type == "heartbeat",
        ).order_by(EventRow.created_at.desc()).first()
        if last_hb is not None:
            last_hb_ts = last_hb[0]
            if last_hb_ts.tzinfo is None:
                last_hb_ts = last_hb_ts.replace(tzinfo=timezone.utc)
            age_s = (now - last_hb_ts).total_seconds()
            if age_s > cfg.heartbeat_stale_after_s:
                emit_event(
                    session,
                    type="reconciler_heartbeat_stale",
                    payload={
                        "last_seen_at": last_hb_ts.isoformat(),
                        "age_seconds": age_s,
                        "threshold_s": cfg.heartbeat_stale_after_s,
                    },
                )
                send_reconcile_heartbeat_stale(
                    last_seen_at=last_hb_ts,
                    age_seconds=age_s,
                    stale_threshold_s=cfg.heartbeat_stale_after_s,
                )

        # 2. Apply tagged fills (or shadow-log them).
        for fill in recent_fills:
            ...
```

You'll also need to add the import at the top of `reconciler/main.py`:

```python
from notifications import send_reconcile_alert, send_reconcile_heartbeat_stale
```

(Replace the existing `from notifications import send_reconcile_alert` line with this longer import.)

And add `EventRow` to the import from `state.mysql_store`:

```python
from state.mysql_store import EventRow, MySQLStore
```

(Find the existing import — there's already one for `MySQLStore`. Some lines down there's a function-local `from state.mysql_store import StrategyRow` — leave that one alone, just add `EventRow` to the top-level import.)

- [ ] **Step 7: Run — expect green**

```bash
docker compose build trader
docker compose run --rm trader pytest tests/test_reconciler_heartbeat_staleness.py tests/test_reconciler_main_loop.py tests/test_reconciler_config.py -v
```

Expected: ALL PASS (3 new staleness tests + 5 main-loop tests still pass + 3 config tests still pass).

If a config test fails: re-check Step 3.

- [ ] **Step 8: Add the env var to `docker-compose.yml`**

In `/Users/alessandro.ren/dev/aitrader/docker-compose.yml`, find the reconciler service block. Add to its `environment:` list, just below `SHADOW_MODE`:

```yaml
      - RECONCILE_HEARTBEAT_STALE_AFTER_S=${RECONCILE_HEARTBEAT_STALE_AFTER_S:-300}
```

- [ ] **Step 9: Final broader test pass**

```bash
docker compose run --rm -e TRADING_ENV=test trader pytest --ignore=tests/test_main_overrides.py 2>&1 | tail -3
```

Expected: green summary.

- [ ] **Step 10: Commit**

```bash
git add notifications.py reconciler/config.py reconciler/main.py docker-compose.yml \
        tests/test_reconciler_config.py tests/test_reconciler_heartbeat_staleness.py
git commit -m "feat(reconciler): heartbeat staleness alert (default 5min)"
```

---

## Task 6: End-to-end smoke

**Files:** none modified.

- [ ] **Step 1: Bring up a fresh stack**

```bash
docker compose down -v
docker compose up -d mysql
until docker compose exec -T mysql mysqladmin ping --silent; do sleep 2; done
docker compose build trader reconciler dashboard
SHADOW_MODE=false docker compose up -d reconciler dashboard
sleep 35  # let one reconciler cycle run
```

- [ ] **Step 2: Insert a synthetic strike to test the CLI**

```bash
docker compose exec -T mysql mysql -u trader -ptraderpass aitrader -e "
INSERT INTO strategies (name) VALUES ('vwap_wave') ON DUPLICATE KEY UPDATE name=name;
INSERT INTO reconciliation_strikes
  (\`key\`, direction, symbol, strategy_id, strike_count, first_seen_at, last_seen_at,
   last_observed_state, resolved)
VALUES
  ('broker_only:TESTSYM', 'broker_only', 'TESTSYM', NULL, 3,
   NOW(3), NOW(3), '{\"mysql_sum\": 0, \"broker_qty\": 1.0}', 0);
"
```

- [ ] **Step 3: Run `list` and `show` from the CLI**

```bash
docker compose run --rm trader python scripts/reconcile_resolve.py list
```

Expected: prints `1 unresolved strike(s):` and the synthetic row's id, direction, symbol.

Capture the strike id from the output (call it `$SID`), then:

```bash
SID=<id from list output>
docker compose run --rm trader python scripts/reconcile_resolve.py show $SID
```

Expected: prints the full detail block with snapshot.

- [ ] **Step 4: Run `dismiss` to resolve the synthetic strike**

```bash
docker compose run --rm trader python scripts/reconcile_resolve.py dismiss $SID --note "smoke test"
```

Expected: `dismissed strike #$SID`.

```bash
docker compose run --rm trader python scripts/reconcile_resolve.py list
```

Expected: `(no unresolved strikes)`.

```bash
docker compose exec -T mysql mysql -u trader -ptraderpass aitrader -e "
SELECT type, payload FROM reconciliation_events
WHERE type = 'operator_action' ORDER BY id DESC LIMIT 1;
"
```

Expected: an `operator_action` row with payload containing `"operator_note": "smoke test"`.

- [ ] **Step 5: Open the dashboard in a browser (optional manual check)**

If your network is set up, browse to the dashboard URL (typically `http://localhost` if nginx is running, or the reverse-proxied address). Check that the **Reconciliation** tab loads, shows a green heartbeat banner, no unresolved strikes, and a recent events feed including your `operator_action` entry from Step 4.

If you can't open a browser, skip — the imports and Streamlit startup were already verified in Task 4 step 4.

- [ ] **Step 6: Tear down**

```bash
docker compose down -v
```

- [ ] **Step 7: No commit needed**

This task is verification-only.

---

## Self-review checklist

**Spec coverage (§5 of the design doc):**
- §5 operator CLI: list / show / close / force-zero / adopt / extend / dismiss → Task 2.
- §5 every action writes a `reconciliation_events` audit row → `resolve_strike` writes one (Task 1, Step 5); `cmd_extend` writes one inline (Task 2).
- §5 dashboard tab: heartbeat freshness / unresolved strikes / events feed → Task 4.
- §5 read-only dashboard, all resolutions through CLI → enforced by the tab having no action buttons.
- §5 heartbeat staleness alert (>5 min) → Task 5.

**Type consistency:**
- `MySQLStore.list_unresolved_strikes`, `get_strike_by_id`, `resolve_strike`, `recent_events`, `events_for_strike`, `insert_adopted_position` defined in Task 1 and consumed by Task 2 + Task 3 with matching signatures.
- `ReconcilerConfig.heartbeat_stale_after_s` field defined in Task 5 step 2 and consumed by Task 5 step 6's main-loop edit.
- `notifications.send_reconcile_heartbeat_stale` signature `(last_seen_at, age_seconds, stale_threshold_s)` defined in Task 5 step 1 and called identically in Task 5 step 6.
- `reconciliation_repo.get_unresolved_strikes` / `get_recent_events` / `get_heartbeat_freshness` defined in Task 3 and consumed by Task 4.

**Placeholder scan:** No TBDs. The only deliberately schematic item is the smoke `<id from list output>` — that's a copy-paste step the operator does at runtime, not a plan placeholder.

**Edge cases:**
- Strike not found → `_get_strike_or_exit` exits with code 2.
- Wrong-direction strike for `close`/`adopt` → `_ensure_direction` exits with code 2.
- Already-resolved strike → `resolve_strike` returns False; CLI prints error.
- Unknown strategy in `adopt` → `_resolve_strategy_id_or_exit` exits with code 2.
- No prior heartbeat → staleness check skips (test 1 of `test_reconciler_heartbeat_staleness.py`).
- Heartbeat exactly at threshold boundary → `>` comparison means equal-to-threshold is not stale.
- SQLite naive datetime in `last_hb_ts` → defensively normalized in main.py edit (matches the strikes.py pattern).

---

## Done When

- All 5 implementation tasks committed (Task 6 is verification).
- `docker compose run --rm -e TRADING_ENV=test trader pytest --ignore=tests/test_main_overrides.py` is green.
- Manual smoke (Task 6) passes: insert → list → show → dismiss → audit-event observed.
- After merge: deploy the new tab + CLI alongside the existing reconciler service. The CLI works against the running MySQL; the dashboard tab shows heartbeat + strikes + events.
- The reconciliation v2 redesign (Plans 1–4) is complete.
