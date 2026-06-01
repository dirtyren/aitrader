"""Tests for broker credentials resolver and MySQLStore CRUD."""
from __future__ import annotations

import logging

import pytest
from sqlalchemy import create_engine

from state.mysql_store import MySQLStore


@pytest.fixture
def store(tmp_path):
    """Build a MySQLStore backed by a temp sqlite DB.

    Bypasses MySQLStore.__init__ because it hardcodes MySQL-only
    connect_args (connect_timeout) that sqlite's DBAPI rejects. The
    methods under test only need `_engine`, so we wire that up directly —
    same pattern as tests/test_schema_broker_credentials.py.
    """
    db_path = tmp_path / "test.db"
    engine = create_engine(f"sqlite:///{db_path}")
    s = MySQLStore.__new__(MySQLStore)
    s._engine = engine
    s.strategy_name = "test"
    s._log = logging.getLogger("test_credentials")
    s._strategy_id = None
    s.ensure_schema()
    return s


def test_get_broker_credentials_missing_returns_none(store):
    assert store.get_broker_credentials("equity") is None


def test_upsert_then_get_broker_credentials(store):
    store.upsert_broker_credentials(
        asset_class="equity",
        api_key="AK1",
        secret_key="SK1",
        base_url="https://paper-api.alpaca.markets",
    )
    row = store.get_broker_credentials("equity")
    assert row is not None
    assert row["api_key"] == "AK1"
    assert row["secret_key"] == "SK1"
    assert row["base_url"] == "https://paper-api.alpaca.markets"
    assert row["account_number"] is None


def test_upsert_updates_existing_and_advances_updated_at(store):
    store.upsert_broker_credentials("equity", "AK1", "SK1", "u")
    first = store.get_broker_credentials("equity")
    assert first is not None
    first_t = first["updated_at"]

    import time
    time.sleep(0.01)

    store.upsert_broker_credentials("equity", "AK2", "SK2", "u2")
    second = store.get_broker_credentials("equity")
    assert second["api_key"] == "AK2"
    assert second["secret_key"] == "SK2"
    assert second["updated_at"] > first_t


def test_set_account_number(store):
    store.upsert_broker_credentials("equity", "AK1", "SK1", "u")
    store.set_broker_credentials_account_number("equity", "ABC1234")
    row = store.get_broker_credentials("equity")
    assert row["account_number"] == "ABC1234"
