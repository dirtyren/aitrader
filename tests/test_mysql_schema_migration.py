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
            raise Exception(f"Duplicate column name (simulated) on: {sql}")

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
    """Duplicate-column errors (expected on already-applied DB) must not raise."""
    store_with_mock_engine._engine = _RecordingEngine(
        fail_on={"ALTER TABLE", "CREATE INDEX"}
    )
    monkeypatch.setattr(Base.metadata, "create_all", lambda _engine: None)
    # Must not raise — duplicate column is the canonical "already applied" case
    store_with_mock_engine.ensure_schema()


def test_ensure_schema_runs_legacy_backfill_after_alters(monkeypatch, store_with_mock_engine):
    """The UPDATE backfill must execute even if some ALTERs fail."""
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
