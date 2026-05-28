# Broker↔MySQL Reconciliation v2 — Plan 1: Schema Migration

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Land the MySQL schema additions required by the reconciliation v2 design, without changing any application logic. After this plan, the database is ready to accept `client_order_id`s, strike rows, and reconciliation events. Application code in later plans will populate them.

**Architecture:** Add columns to `positions` and `trades`; add two new tables `reconciliation_strikes` and `reconciliation_events`. Reflect the new shape in both `state/schema.sql` (used at MySQL container init) and `state/mysql_store.py` ORM models / `ensure_schema()` migrations (used at trader-container startup against an existing DB). Backfill `legacy_untagged=TRUE` for rows already open at migration time so the reconciler service in Plan 3 treats them as alert-only.

**Tech Stack:** MySQL 8.0, SQLAlchemy 2.x ORM (DeclarativeBase / Mapped), pytest, the existing `_StubStore` test pattern from `tests/test_mysql_legacy_migration.py`.

**Spec:** `docs/superpowers/specs/2026-05-28-broker-mysql-reconciliation-design.md` §4 (schema changes) and §3 (strike table shape).

---

## File Structure

**Modify:**
- `state/schema.sql` — add columns to `positions` / `trades`; add `reconciliation_strikes` and `reconciliation_events` tables. This file is read by the `mysql` container's init scripts on a *fresh* volume, so it must reflect the final shape.
- `state/mysql_store.py`
  - ORM `PositionRow` — add `client_order_id`, `exit_client_order_id`, `legacy_untagged`.
  - ORM `TradeRow` — add `client_order_id`, `exit_client_order_id`.
  - New ORM `StrikeRow` for `reconciliation_strikes`.
  - New ORM `EventRow` for `reconciliation_events`.
  - `ensure_schema()` — add idempotent `ALTER TABLE … ADD COLUMN …` for existing-DB upgrades, and a one-shot backfill of `legacy_untagged=TRUE` for rows where `client_order_id IS NULL` and `status='open'`.

**Create:**
- `tests/test_mysql_schema_migration.py` — unit tests for the migration logic against a SQLite in-memory engine (not a real MySQL — tests assert SQLAlchemy `create_all` produces the expected tables/columns and that `ensure_schema()` is idempotent).

**Untouched in this plan (later plans):**
- `OrderExecutor` / `AlpacaClient` (Plan 2).
- The `reconciler/` service module (Plan 3).
- `scripts/reconcile_resolve.py` and dashboard tab (Plan 4).

---

## Task 1: Add columns to `state/schema.sql` for fresh-DB installs

**Files:**
- Modify: `state/schema.sql`

This is the file the MySQL container reads when initializing a *fresh* `db_data` volume. It must reflect the post-migration shape so a clean install matches an upgraded one.

- [ ] **Step 1: Read the current file**

Read `state/schema.sql` to confirm structure.

- [ ] **Step 2: Add three new columns to the `positions` table**

Replace the `positions` table block in `state/schema.sql` with the version below. The change adds `client_order_id`, `exit_client_order_id`, and `legacy_untagged` after `stop_order_id`. All three are nullable / default safe so they don't break existing inserts that don't yet supply them.

```sql
CREATE TABLE IF NOT EXISTS positions (
    id              INT AUTO_INCREMENT PRIMARY KEY,
    strategy_id     INT NOT NULL,
    symbol          VARCHAR(32) NOT NULL,
    asset_class     VARCHAR(16) NOT NULL,        -- 'equity' or 'crypto'
    side            VARCHAR(8) NOT NULL,          -- 'long' or 'short'
    qty             DECIMAL(20,8) NOT NULL,
    entry_px        DECIMAL(20,8) NOT NULL,
    stop_px         DECIMAL(20,8) DEFAULT NULL,
    target_px       DECIMAL(20,8) DEFAULT NULL,
    initial_stop_px DECIMAL(20,8) DEFAULT NULL,
    setup_name      VARCHAR(64) NOT NULL,         -- e.g. 'vwap_bounce', 'adopted'
    order_id        VARCHAR(64) DEFAULT '',
    stop_order_id   VARCHAR(64) DEFAULT NULL,
    client_order_id      VARCHAR(128) DEFAULT NULL,  -- entry COID, set on position_opened
    exit_client_order_id VARCHAR(128) DEFAULT NULL,  -- exit COID, set on position_closed
    legacy_untagged      TINYINT(1) DEFAULT 0,       -- 1 = pre-COID-migration row; reconciler treats as alert-only
    breakeven_moved TINYINT(1) DEFAULT 0,
    bars_held       INT DEFAULT 0,
    adopted         TINYINT(1) DEFAULT 0,
    status          ENUM('open', 'closed') NOT NULL DEFAULT 'open',
    opened_at       TIMESTAMP(3) NOT NULL,
    closed_at       TIMESTAMP(3) DEFAULT NULL,
    close_reason    VARCHAR(32) DEFAULT NULL,
    exit_px         DECIMAL(20,8) DEFAULT NULL,
    pnl_usd         DECIMAL(20,8) DEFAULT NULL,
    R_realized      DECIMAL(20,8) DEFAULT NULL,
    FOREIGN KEY (strategy_id) REFERENCES strategies(id),
    INDEX idx_open (strategy_id, status, symbol),
    INDEX idx_closed_time (strategy_id, closed_at),
    INDEX idx_client_order_id (client_order_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

- [ ] **Step 3: Add two new columns to the `trades` table**

Replace the `trades` table block with this version (adds `client_order_id`, `exit_client_order_id`):

```sql
CREATE TABLE IF NOT EXISTS trades (
    id              INT AUTO_INCREMENT PRIMARY KEY,
    strategy_id     INT NOT NULL,
    symbol          VARCHAR(32) NOT NULL,
    asset_class     VARCHAR(16) NOT NULL,
    setup_name      VARCHAR(64) NOT NULL,
    side            VARCHAR(8) NOT NULL,
    qty             DECIMAL(20,8) NOT NULL,
    entry_px        DECIMAL(20,8) NOT NULL,
    exit_px         DECIMAL(20,8) NOT NULL,
    stop_px         DECIMAL(20,8) DEFAULT NULL,
    target_px       DECIMAL(20,8) DEFAULT NULL,
    initial_stop_px DECIMAL(20,8) DEFAULT NULL,
    pnl_usd         DECIMAL(20,8) NOT NULL,
    R_realized      DECIMAL(20,8) NOT NULL,
    close_reason    VARCHAR(32) NOT NULL,
    opened_at       TIMESTAMP(3) NOT NULL,
    closed_at       TIMESTAMP(3) NOT NULL,
    bars_held       INT DEFAULT 0,
    reflected       TINYINT(1) DEFAULT 0,
    client_order_id      VARCHAR(128) DEFAULT NULL,
    exit_client_order_id VARCHAR(128) DEFAULT NULL,
    FOREIGN KEY (strategy_id) REFERENCES strategies(id),
    INDEX idx_trades_time (strategy_id, closed_at),
    INDEX idx_trades_symbol (strategy_id, symbol),
    INDEX idx_trades_client_order_id (client_order_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

- [ ] **Step 4: Append two new tables at the end of `state/schema.sql`**

Add these blocks after the `daily_stats` table:

```sql
CREATE TABLE IF NOT EXISTS reconciliation_strikes (
    id                   BIGINT AUTO_INCREMENT PRIMARY KEY,
    `key`                VARCHAR(128) NOT NULL,
    direction            ENUM('qty_drift','mysql_only','broker_only') NOT NULL,
    strategy_id          INT DEFAULT NULL,
    symbol               VARCHAR(32) NOT NULL,
    strike_count         INT NOT NULL DEFAULT 0,
    first_seen_at        TIMESTAMP(3) NOT NULL,
    last_seen_at         TIMESTAMP(3) NOT NULL,
    last_observed_state  JSON DEFAULT NULL,
    resolved             TINYINT(1) NOT NULL DEFAULT 0,
    resolved_at          TIMESTAMP(3) DEFAULT NULL,
    resolved_reason      VARCHAR(64) DEFAULT NULL,
    FOREIGN KEY (strategy_id) REFERENCES strategies(id),
    UNIQUE KEY uq_open_key (`key`, resolved),
    INDEX idx_strikes_unresolved (resolved, last_seen_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS reconciliation_events (
    id            BIGINT AUTO_INCREMENT PRIMARY KEY,
    type          VARCHAR(32) NOT NULL,                 -- 'heartbeat','untagged_fill','mysql_only_confirmed','broker_only_confirmed','operator_action','tagged_fill_applied','tagged_entry_inserted'
    strategy_id   INT DEFAULT NULL,
    symbol        VARCHAR(32) DEFAULT NULL,
    payload       JSON DEFAULT NULL,
    created_at    TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    FOREIGN KEY (strategy_id) REFERENCES strategies(id),
    INDEX idx_events_time (created_at),
    INDEX idx_events_type (type, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

Notes for the engineer:
- `uq_open_key` uses `(key, resolved)` so an unresolved row is unique per key, but historic resolved rows are unbounded. (MySQL's `UNIQUE KEY` over a `TINYINT` resolved flag is fine — multiple `resolved=1` rows for the same key coexist; only one `resolved=0` row per key at a time.)
- `key` is a reserved word in some SQL contexts; we backtick-quote it. SQLAlchemy will quote it the same way.
- `payload` is `JSON` so we can dump arbitrary snapshots without schema churn.

- [ ] **Step 5: Commit the schema file change**

```bash
git add state/schema.sql
git commit -m "schema: add client_order_id columns and reconciliation tables (fresh-DB shape)"
```

---

## Task 2: Update ORM models in `state/mysql_store.py`

**Files:**
- Modify: `state/mysql_store.py:57-89` (`PositionRow`)
- Modify: `state/mysql_store.py:91-118` (`TradeRow`)
- Modify: `state/mysql_store.py:43-45` and below (add `StrikeRow`, `EventRow` after `TradeRow`)

We update SQLAlchemy ORM models so `Base.metadata.create_all(engine)` builds the new tables/columns at trader-container startup against a *fresh* DB volume (matches Task 1's schema.sql). Existing-DB upgrades are handled in Task 3 via `ALTER TABLE`.

- [ ] **Step 1: Read current PositionRow / TradeRow definitions**

Read lines 57–118 of `state/mysql_store.py` to confirm column ordering before editing.

- [ ] **Step 2: Add three columns to `PositionRow`**

In `PositionRow` (currently at line 57), after the `stop_order_id` field (line 72), add:

```python
    client_order_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True, index=True)
    exit_client_order_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    legacy_untagged: Mapped[bool] = mapped_column(Boolean, default=False)
```

Also extend `__table_args__` (currently `(Index("idx_open", "strategy_id", "status", "symbol"),)`) to add an explicit named index over `client_order_id`:

```python
    __table_args__ = (
        Index("idx_open", "strategy_id", "status", "symbol"),
        Index("idx_client_order_id", "client_order_id"),
    )
```

- [ ] **Step 3: Add two columns to `TradeRow`**

In `TradeRow` (currently at line 91), after the `reflected` field (line 112), add:

```python
    client_order_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True, index=True)
    exit_client_order_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
```

Extend `__table_args__` to:

```python
    __table_args__ = (
        Index("idx_trades_time", "strategy_id", "closed_at"),
        Index("idx_trades_symbol", "strategy_id", "symbol"),
        Index("idx_trades_client_order_id", "client_order_id"),
    )
```

- [ ] **Step 4: Add `StrikeRow` and `EventRow` ORM classes**

After the `TradeRow` class block (immediately before the `# ── Store ──…` comment at line 120), add:

```python
class StrikeRow(Base):
    __tablename__ = "reconciliation_strikes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # 'key' collides with SQL keyword in some dialects; SQLAlchemy quotes it.
    key: Mapped[str] = mapped_column("key", String(128), nullable=False)
    direction: Mapped[str] = mapped_column(
        Enum("qty_drift", "mysql_only", "broker_only"), nullable=False
    )
    strategy_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("strategies.id"), nullable=True
    )
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    strike_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_observed_state: Mapped[Optional[str]] = mapped_column(JSON, nullable=True)
    resolved: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_reason: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    __table_args__ = (
        UniqueConstraint("key", "resolved", name="uq_open_key"),
        Index("idx_strikes_unresolved", "resolved", "last_seen_at"),
    )


class EventRow(Base):
    __tablename__ = "reconciliation_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    type: Mapped[str] = mapped_column(String(32), nullable=False)
    strategy_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("strategies.id"), nullable=True
    )
    symbol: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    payload: Mapped[Optional[str]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP(3)")
    )

    __table_args__ = (
        Index("idx_events_time", "created_at"),
        Index("idx_events_type", "type", "created_at"),
    )
```

- [ ] **Step 5: Add the missing imports**

At the top of `state/mysql_store.py`, the current imports almost certainly do not yet include `JSON` or `UniqueConstraint`. Locate the SQLAlchemy import block (it is the line that starts `from sqlalchemy import …` near the top of the file). Add `JSON` and `UniqueConstraint` to that import list. If `text` is not already imported (it is used on line 168 of the existing code, so it is), leave it alone.

Concretely the SQLAlchemy import line should now include at least: `Boolean, DateTime, Enum, ForeignKey, Index, Integer, JSON, Numeric, String, UniqueConstraint, text`.

Do not change unrelated imports.

- [ ] **Step 6: Run the existing test suite to confirm no regressions from ORM changes**

```bash
pytest -x tests/test_mysql_legacy_migration.py -v
```

Expected: PASS (these tests stub the engine, so they exercise constructor/import wiring only).

```bash
pytest -x -k "not integration" -v
```

Expected: full suite green. Any failure here is unrelated to schema and must be fixed before continuing.

- [ ] **Step 7: Commit**

```bash
git add state/mysql_store.py
git commit -m "schema(orm): add client_order_id columns and StrikeRow/EventRow models"
```

---

## Task 3: Add idempotent `ALTER TABLE` upgrades to `ensure_schema()`

**Files:**
- Modify: `state/mysql_store.py` — `ensure_schema()` method (currently at line 162).

`Base.metadata.create_all(engine)` only creates tables/columns when the table doesn't yet exist. For a DB that already contains `positions` and `trades`, it will not add the new columns — so we need explicit `ALTER TABLE`s. This mirrors the existing `reflected` migration (`mysql_store.py:166-173`).

- [ ] **Step 1: Read the current `ensure_schema` method**

Re-read `state/mysql_store.py:162-173` to confirm the existing pattern (try/except around an ALTER, swallow the duplicate-column error).

- [ ] **Step 2: Replace `ensure_schema()` with the version below**

```python
    def ensure_schema(self) -> None:
        """Create tables if they don't exist. Idempotent. Also applies migrations.

        For existing DBs we cannot rely on create_all to add new columns, so each
        new column gets its own try/except ALTER. Order matters only for the
        legacy_untagged backfill (must run after the column exists).
        """
        Base.metadata.create_all(self._engine)

        migrations: list[str] = [
            # trades.reflected — historic, kept for backwards compat
            "ALTER TABLE trades ADD COLUMN reflected TINYINT(1) DEFAULT 0",
            # positions: client_order_id columns + legacy_untagged
            "ALTER TABLE positions ADD COLUMN client_order_id VARCHAR(128) DEFAULT NULL",
            "ALTER TABLE positions ADD COLUMN exit_client_order_id VARCHAR(128) DEFAULT NULL",
            "ALTER TABLE positions ADD COLUMN legacy_untagged TINYINT(1) DEFAULT 0",
            "CREATE INDEX idx_client_order_id ON positions (client_order_id)",
            # trades: client_order_id columns
            "ALTER TABLE trades ADD COLUMN client_order_id VARCHAR(128) DEFAULT NULL",
            "ALTER TABLE trades ADD COLUMN exit_client_order_id VARCHAR(128) DEFAULT NULL",
            "CREATE INDEX idx_trades_client_order_id ON trades (client_order_id)",
        ]
        for stmt in migrations:
            try:
                with self._engine.connect() as conn:
                    conn.execute(text(stmt))
                    conn.commit()
            except Exception:
                # Column / index already exists, or DB is fresh and create_all
                # already provisioned it. Either is fine.
                pass

        # One-shot backfill: any row currently open with no client_order_id
        # is a pre-migration legacy position. Mark it so the reconciler service
        # in Plan 3 treats it as alert-only and never auto-mutates it.
        try:
            with self._engine.connect() as conn:
                conn.execute(text(
                    "UPDATE positions "
                    "SET legacy_untagged = 1 "
                    "WHERE status = 'open' "
                    "AND client_order_id IS NULL "
                    "AND legacy_untagged = 0"
                ))
                conn.commit()
        except Exception as exc:
            self._log.warning("MYSQL_LEGACY_BACKFILL_FAILED: %s", exc)
```

Reasoning notes:
- Keeping the historic `reflected` ALTER inline rather than removing it: Plan 1 doesn't claim to be a refactor; the existing migration must still run on DBs upgraded before this plan landed.
- Indexes are created via separate `CREATE INDEX` rather than as part of the `ALTER TABLE ADD COLUMN` because MySQL accepts both, and separating them makes the swallow-on-already-exists case granular per index.
- The backfill is idempotent (`legacy_untagged = 0` filter); running it on every startup is cheap on the open-positions index.

- [ ] **Step 3: Run the existing test suite again**

```bash
pytest -x -k "not integration" -v
```

Expected: PASS. The change does not affect existing tests; it only adds new ALTER statements that the existing tests' stubbed engine never executes.

- [ ] **Step 4: Commit**

```bash
git add state/mysql_store.py
git commit -m "schema: idempotent ALTER migrations + legacy_untagged backfill in ensure_schema"
```

---

## Task 4: Write the schema migration test against an in-memory engine

**Files:**
- Create: `tests/test_mysql_schema_migration.py`

We use SQLAlchemy's SQLite in-memory engine to verify `Base.metadata.create_all` and the ORM classes work end-to-end without a real MySQL container. `ensure_schema()`'s ALTER statements include MySQL-specific syntax (`TINYINT`, `INDEX` syntax differences), so the ALTER path is exercised separately by mocking the engine — see step 4.

- [ ] **Step 1: Write the test file with the test stubs**

Create `tests/test_mysql_schema_migration.py` with this content:

```python
"""Tests for MySQLStore schema migrations.

In-memory SQLite is used to exercise the ORM's create_all path. The MySQL-only
ALTER statements in ensure_schema() are tested via a mocked engine that records
the statements it would execute.
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine, inspect

from state.mysql_store import (
    Base,
    PositionRow,
    TradeRow,
    StrikeRow,
    EventRow,
    MySQLStore,
)


# ── Helpers ────────────────────────────────────────────────────────────


def _sqlite_engine():
    """Fresh in-memory SQLite engine; creates all ORM tables."""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return engine


# ── ORM-level tests (real engine, real CREATE) ─────────────────────────


def test_create_all_creates_strike_and_event_tables():
    engine = _sqlite_engine()
    insp = inspect(engine)
    tables = set(insp.get_table_names())
    assert "reconciliation_strikes" in tables
    assert "reconciliation_events" in tables


def test_position_row_has_client_order_id_columns():
    engine = _sqlite_engine()
    cols = {c["name"] for c in inspect(engine).get_columns("positions")}
    assert "client_order_id" in cols
    assert "exit_client_order_id" in cols
    assert "legacy_untagged" in cols


def test_trade_row_has_client_order_id_columns():
    engine = _sqlite_engine()
    cols = {c["name"] for c in inspect(engine).get_columns("trades")}
    assert "client_order_id" in cols
    assert "exit_client_order_id" in cols


def test_strike_row_columns():
    engine = _sqlite_engine()
    cols = {c["name"] for c in inspect(engine).get_columns("reconciliation_strikes")}
    expected = {
        "id", "key", "direction", "strategy_id", "symbol",
        "strike_count", "first_seen_at", "last_seen_at",
        "last_observed_state", "resolved", "resolved_at", "resolved_reason",
    }
    assert expected.issubset(cols), f"missing: {expected - cols}"


def test_event_row_columns():
    engine = _sqlite_engine()
    cols = {c["name"] for c in inspect(engine).get_columns("reconciliation_events")}
    expected = {"id", "type", "strategy_id", "symbol", "payload", "created_at"}
    assert expected.issubset(cols), f"missing: {expected - cols}"


def test_create_all_is_idempotent():
    """Running create_all twice is a no-op (idempotent table creation)."""
    engine = _sqlite_engine()
    # Second call should not raise
    Base.metadata.create_all(engine)
    insp = inspect(engine)
    assert "reconciliation_strikes" in set(insp.get_table_names())


# ── ensure_schema() ALTER path with mocked engine ──────────────────────


class _RecordingConnection:
    """Stub connection that records SQL strings instead of executing them."""

    def __init__(self, fail_on: set[str] | None = None):
        self.executed: list[str] = []
        self.committed: int = 0
        self._fail_on = fail_on or set()

    def execute(self, stmt):
        sql = str(stmt)
        self.executed.append(sql)
        if any(token in sql for token in self._fail_on):
            raise Exception(f"simulated DB error on: {sql}")

    def commit(self):
        self.committed += 1

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class _RecordingEngine:
    """Minimal SQLAlchemy-engine stand-in for ensure_schema's ALTER pass."""

    def __init__(self, fail_on: set[str] | None = None):
        self.conn = _RecordingConnection(fail_on=fail_on)

    def connect(self):
        return self.conn


@pytest.fixture
def store_with_mock_engine():
    store = MySQLStore.__new__(MySQLStore)  # bypass __init__ (no real DB)
    store.strategy_name = "test"
    store._strategy_id = None
    store._log = __import__("logging").getLogger("test")
    store._engine = _RecordingEngine()
    return store


def test_ensure_schema_executes_all_expected_alters(monkeypatch, store_with_mock_engine):
    """ensure_schema must attempt the column-add and index ALTERs we declared."""
    # Bypass create_all on the fake engine
    monkeypatch.setattr(Base.metadata, "create_all", lambda _engine: None)
    store_with_mock_engine.ensure_schema()

    executed = "\n".join(store_with_mock_engine._engine.conn.executed)
    # Spot-check critical statements
    assert "ALTER TABLE positions ADD COLUMN client_order_id" in executed
    assert "ALTER TABLE positions ADD COLUMN exit_client_order_id" in executed
    assert "ALTER TABLE positions ADD COLUMN legacy_untagged" in executed
    assert "ALTER TABLE trades ADD COLUMN client_order_id" in executed
    assert "ALTER TABLE trades ADD COLUMN exit_client_order_id" in executed
    assert "CREATE INDEX idx_client_order_id ON positions" in executed
    assert "CREATE INDEX idx_trades_client_order_id ON trades" in executed
    # Backfill must run
    assert "UPDATE positions" in executed and "legacy_untagged = 1" in executed


def test_ensure_schema_swallows_duplicate_column_errors(monkeypatch, store_with_mock_engine):
    """Each ALTER may fail (column already exists) — must not raise."""
    # All ALTERs raise; ensure_schema must complete without error
    store_with_mock_engine._engine = _RecordingEngine(
        fail_on={"ALTER TABLE", "CREATE INDEX"}
    )
    monkeypatch.setattr(Base.metadata, "create_all", lambda _engine: None)
    # Must not raise
    store_with_mock_engine.ensure_schema()


def test_ensure_schema_runs_legacy_backfill_after_alters(monkeypatch, store_with_mock_engine):
    """The UPDATE backfill must execute even if some ALTERs fail."""
    # Fail only on ALTERs/CREATE INDEXes; allow UPDATE through
    store_with_mock_engine._engine = _RecordingEngine(
        fail_on={"ALTER TABLE", "CREATE INDEX"}
    )
    monkeypatch.setattr(Base.metadata, "create_all", lambda _engine: None)
    store_with_mock_engine.ensure_schema()

    executed = store_with_mock_engine._engine.conn.executed
    update_idx = next(
        (i for i, s in enumerate(executed) if "UPDATE positions" in s),
        None,
    )
    assert update_idx is not None, "legacy backfill UPDATE was not executed"
```

- [ ] **Step 2: Run the new test file — expect FAILS to pass first**

```bash
pytest tests/test_mysql_schema_migration.py -v
```

Expected: ALL TESTS PASS. (Tasks 2 and 3 already implemented the ORM and ensure_schema changes the tests cover.)

If any test fails:
- `ImportError` on `StrikeRow` / `EventRow` → revisit Task 2 step 4.
- `KeyError "client_order_id"` on a column inspect → revisit Task 2 step 2 / step 3.
- `assert ALTER TABLE …` failure → revisit Task 3 step 2's `migrations` list.

Fix the cause, re-run.

- [ ] **Step 3: Run the full test suite to confirm no regressions**

```bash
pytest -x -k "not integration" -v
```

Expected: PASS, including the new `test_mysql_schema_migration.py` tests.

- [ ] **Step 4: Commit**

```bash
git add tests/test_mysql_schema_migration.py
git commit -m "test: schema migration adds COID columns + reconciliation tables"
```

---

## Task 5: Verify against a real MySQL container (manual smoke)

This task is the final guard before merge. The unit tests in Task 4 use SQLite (no MySQL-specific dialect), so we manually run the migration against a real MySQL container with simulated existing data.

**Files:** none modified.

- [ ] **Step 1: Bring up the MySQL container fresh**

```bash
docker compose down -v  # destroys db_data volume
docker compose up -d mysql
docker compose logs -f mysql 2>&1 | head -40
```

Wait until you see `ready for connections`. The `state/schema.sql` from Task 1 runs automatically on first init.

- [ ] **Step 2: Connect and confirm fresh-DB shape**

```bash
docker compose exec mysql mysql -u trader -ptraderpass aitrader -e "DESCRIBE positions;"
docker compose exec mysql mysql -u trader -ptraderpass aitrader -e "DESCRIBE trades;"
docker compose exec mysql mysql -u trader -ptraderpass aitrader -e "SHOW TABLES;"
```

Expected:
- `positions` includes `client_order_id`, `exit_client_order_id`, `legacy_untagged`.
- `trades` includes `client_order_id`, `exit_client_order_id`.
- `SHOW TABLES` lists `reconciliation_strikes` and `reconciliation_events`.

- [ ] **Step 3: Simulate an existing-DB upgrade**

Drop the new columns/tables to simulate a pre-migration DB, then run `ensure_schema()` to confirm the ALTER path adds them back:

```bash
docker compose exec mysql mysql -u trader -ptraderpass aitrader -e "
ALTER TABLE positions DROP COLUMN client_order_id;
ALTER TABLE positions DROP COLUMN exit_client_order_id;
ALTER TABLE positions DROP COLUMN legacy_untagged;
ALTER TABLE trades DROP COLUMN client_order_id;
ALTER TABLE trades DROP COLUMN exit_client_order_id;
DROP TABLE reconciliation_strikes;
DROP TABLE reconciliation_events;
"
```

Insert a test "legacy" open row so the backfill has something to update:

```bash
docker compose exec mysql mysql -u trader -ptraderpass aitrader -e "
INSERT INTO strategies (name) VALUES ('test_legacy');
INSERT INTO positions
  (strategy_id, symbol, asset_class, side, qty, entry_px, setup_name, opened_at, status)
VALUES
  (LAST_INSERT_ID(), 'AAPL', 'equity', 'long', 1.0, 100.0, 'vwap_bounce', NOW(3), 'open');
"
```

- [ ] **Step 4: Run `ensure_schema()` from a Python REPL inside the trader image**

```bash
docker compose run --rm trader python -c "
from state.mysql_store import MySQLStore
import logging
logging.basicConfig(level='INFO')
store = MySQLStore('test_legacy')
store.ensure_schema()
print('OK')
"
```

Expected: prints `OK`. No tracebacks. Warnings about duplicate columns are not expected because we just dropped them; the ALTERs should succeed cleanly.

- [ ] **Step 5: Verify migration produced the expected state**

```bash
docker compose exec mysql mysql -u trader -ptraderpass aitrader -e "
DESCRIBE positions;
SELECT id, symbol, status, client_order_id, legacy_untagged FROM positions;
SHOW TABLES LIKE 'reconciliation_%';
"
```

Expected:
- `positions` has all three new columns again.
- The test row has `client_order_id IS NULL` and `legacy_untagged = 1` (the backfill flagged it).
- Both `reconciliation_strikes` and `reconciliation_events` exist.

- [ ] **Step 6: Verify idempotency by running `ensure_schema()` a second time**

```bash
docker compose run --rm trader python -c "
from state.mysql_store import MySQLStore
store = MySQLStore('test_legacy')
store.ensure_schema()
store.ensure_schema()
print('OK twice')
"
```

Expected: prints `OK twice`. No errors raised — the ALTER swallowing handles the now-existing-columns case.

- [ ] **Step 7: Tear down the test data**

```bash
docker compose exec mysql mysql -u trader -ptraderpass aitrader -e "
DELETE FROM positions WHERE setup_name = 'vwap_bounce' AND symbol = 'AAPL' AND strategy_id IN (SELECT id FROM strategies WHERE name = 'test_legacy');
DELETE FROM strategies WHERE name = 'test_legacy';
"
```

(Or `docker compose down -v` if you want a fully clean slate.)

- [ ] **Step 8: No commit needed**

This task is a manual smoke verification; no files changed. Note in the PR description that all 8 steps of Task 5 passed.

---

## Self-review checklist

**Spec coverage:**
- §3 strike table → Task 1 step 4 + Task 2 step 4 ✓
- §3 events table → Task 1 step 4 + Task 2 step 4 ✓
- §4 `client_order_id` columns on positions/trades → Task 1 steps 2–3 + Task 2 steps 2–3 ✓
- §4 `legacy_untagged` flag + backfill → Task 1 step 2 + Task 3 step 2 ✓
- §4 idempotent `ensure_schema()` for existing DBs → Task 3 + Task 4 step 1 + Task 5 step 6 ✓

**Out of scope (deferred to later plans, intentionally):**
- Helper methods on `MySQLStore` for strike/event CRUD → Plan 3 (reconciler service).
- `OrderExecutor` writing the `client_order_id` column on `position_opened` → Plan 2.
- Plan 4 (operator CLI + dashboard) reads strikes/events; the schema is ready for it.

**Type consistency:** `StrikeRow.key` (mapped to SQL column `key`), `direction` enum values (`qty_drift`, `mysql_only`, `broker_only`), `EventRow.type` value strings — all match the spec § 3 / § 5.

**Placeholder scan:** No TBDs, TODOs, or "fill in details". Every step has runnable code or commands.

---

## Done When

- All 5 tasks committed (Tasks 1–4 produce commits; Task 5 is a manual smoke).
- `pytest -x -k "not integration" -v` passes.
- Manual smoke against a real MySQL container in Task 5 passes.
- The branch is mergeable to `main`. After merge, Plan 2 (`client_order_id` contract in `OrderExecutor`) becomes implementable.
