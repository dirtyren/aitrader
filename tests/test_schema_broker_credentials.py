"""Schema test for broker_credentials table."""
from __future__ import annotations

import os
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine, text

from state.mysql_store import MySQLStore


@pytest.fixture
def store(tmp_path, monkeypatch):
    """Build a MySQLStore backed by a temp sqlite DB.

    Bypasses MySQLStore.__init__ because it hardcodes MySQL-only
    connect_args (connect_timeout) that sqlite's DBAPI rejects. The
    schema-creation path under test (ensure_schema → Base.metadata.create_all)
    only needs `_engine`, so we wire that up directly — same pattern as
    tests/test_mysql_store_coid.py.
    """
    import logging
    db_path = tmp_path / "test.db"
    engine = create_engine(f"sqlite:///{db_path}")
    s = MySQLStore.__new__(MySQLStore)
    s._engine = engine
    s.strategy_name = "test"
    s._log = logging.getLogger("test_broker_credentials")
    s._strategy_id = None
    s.ensure_schema()
    return s


def test_broker_credentials_table_exists(store):
    with store._engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='broker_credentials'"
        )).fetchall()
    assert len(rows) == 1


def test_broker_credentials_has_expected_columns(store):
    with store._engine.connect() as conn:
        rows = conn.execute(text(
            "PRAGMA table_info(broker_credentials)"
        )).fetchall()
    col_names = {r[1] for r in rows}
    assert col_names >= {
        "asset_class", "api_key", "secret_key", "base_url",
        "account_number", "updated_at",
    }


def test_broker_credentials_roundtrip(store):
    from datetime import datetime
    with store._engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO broker_credentials "
            "(asset_class, api_key, secret_key, base_url, account_number, updated_at) "
            "VALUES (:ac, :k, :s, :u, :an, :t)"
        ), {
            "ac": "equity", "k": "AK1", "s": "SK1",
            "u": "https://paper-api.alpaca.markets",
            "an": "ABC1234", "t": datetime.utcnow(),
        })

    with store._engine.connect() as conn:
        row = conn.execute(text(
            "SELECT api_key, secret_key, base_url, account_number "
            "FROM broker_credentials WHERE asset_class = 'equity'"
        )).fetchone()
    assert row is not None
    assert row[0] == "AK1"
    assert row[1] == "SK1"
    assert row[2] == "https://paper-api.alpaca.markets"
    assert row[3] == "ABC1234"
