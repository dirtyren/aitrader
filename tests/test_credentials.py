"""Tests for broker credentials resolver and MySQLStore CRUD."""
from __future__ import annotations

import logging

import pytest
import requests
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


# ---------------------------------------------------------------------------
# Resolver tests
# ---------------------------------------------------------------------------

from broker import credentials as creds_mod
from broker.credentials import (
    AlpacaCreds, MissingCredentialsError, resolve, upsert,
)


@pytest.fixture
def clean_env(monkeypatch):
    """Strip every Alpaca-related env var so each test starts blank."""
    for k in [
        "ALPACA_API_KEY", "ALPACA_SECRET_KEY", "ALPACA_BASE_URL",
        "ALPACA_EQUITY_API_KEY", "ALPACA_EQUITY_SECRET_KEY", "ALPACA_EQUITY_BASE_URL",
        "ALPACA_CRYPTO_API_KEY", "ALPACA_CRYPTO_SECRET_KEY", "ALPACA_CRYPTO_BASE_URL",
    ]:
        monkeypatch.delenv(k, raising=False)


@pytest.fixture
def patched_store(store, monkeypatch):
    """Make the resolver's get_store() return our test store."""
    monkeypatch.setattr(creds_mod, "_get_store", lambda: store)
    return store


def test_resolve_invalid_asset_class_raises():
    with pytest.raises(ValueError):
        resolve("options")


def test_resolve_returns_db_row_when_present(clean_env, patched_store):
    patched_store.upsert_broker_credentials(
        "equity", "AK_DB", "SK_DB", "https://db.example",
    )
    out = resolve("equity")
    assert out.api_key == "AK_DB"
    assert out.secret_key == "SK_DB"
    assert out.base_url == "https://db.example"
    assert out.source == "db"


def test_resolve_falls_back_to_split_env_and_seeds_db(
    clean_env, patched_store, monkeypatch,
):
    monkeypatch.setenv("ALPACA_EQUITY_API_KEY", "AK_ENV")
    monkeypatch.setenv("ALPACA_EQUITY_SECRET_KEY", "SK_ENV")
    monkeypatch.setenv("ALPACA_EQUITY_BASE_URL", "https://env.example")

    out = resolve("equity")
    assert out.source == "env_bootstrap"
    assert out.api_key == "AK_ENV"

    # DB now seeded
    row = patched_store.get_broker_credentials("equity")
    assert row is not None
    assert row["api_key"] == "AK_ENV"


def test_resolve_split_env_default_base_url(clean_env, patched_store, monkeypatch):
    monkeypatch.setenv("ALPACA_EQUITY_API_KEY", "AK_ENV")
    monkeypatch.setenv("ALPACA_EQUITY_SECRET_KEY", "SK_ENV")
    out = resolve("equity")
    assert out.base_url == "https://paper-api.alpaca.markets"


def test_resolve_legacy_env_used_when_split_absent(
    clean_env, patched_store, monkeypatch,
):
    monkeypatch.setenv("ALPACA_API_KEY", "AK_LEGACY")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "SK_LEGACY")
    out_e = resolve("equity")
    out_c = resolve("crypto")
    assert out_e.source == "env_legacy"
    assert out_c.source == "env_legacy"
    assert out_e.api_key == "AK_LEGACY"
    assert out_c.api_key == "AK_LEGACY"
    # Legacy fallback should NOT seed the DB.
    assert patched_store.get_broker_credentials("equity") is None
    assert patched_store.get_broker_credentials("crypto") is None


def test_resolve_split_env_takes_precedence_over_legacy(
    clean_env, patched_store, monkeypatch,
):
    monkeypatch.setenv("ALPACA_API_KEY", "AK_LEGACY")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "SK_LEGACY")
    monkeypatch.setenv("ALPACA_EQUITY_API_KEY", "AK_SPLIT")
    monkeypatch.setenv("ALPACA_EQUITY_SECRET_KEY", "SK_SPLIT")
    out = resolve("equity")
    assert out.api_key == "AK_SPLIT"
    assert out.source == "env_bootstrap"


def test_resolve_db_takes_precedence_over_env(clean_env, patched_store, monkeypatch):
    monkeypatch.setenv("ALPACA_EQUITY_API_KEY", "AK_ENV")
    monkeypatch.setenv("ALPACA_EQUITY_SECRET_KEY", "SK_ENV")
    patched_store.upsert_broker_credentials(
        "equity", "AK_DB", "SK_DB", "https://db.example",
    )
    out = resolve("equity")
    assert out.api_key == "AK_DB"
    assert out.source == "db"


def test_resolve_raises_when_nothing_configured(clean_env, patched_store):
    with pytest.raises(MissingCredentialsError):
        resolve("equity")


def test_resolve_treats_db_row_with_empty_key_as_missing(
    clean_env, patched_store, monkeypatch,
):
    # Manually insert empty-string row.
    from datetime import datetime
    from sqlalchemy import text as sql_text
    with patched_store._engine.begin() as conn:
        conn.execute(sql_text(
            "INSERT INTO broker_credentials "
            "(asset_class, api_key, secret_key, base_url, account_number, updated_at) "
            "VALUES ('equity', '', '', 'u', NULL, :t)"
        ), {"t": datetime.utcnow()})
    monkeypatch.setenv("ALPACA_EQUITY_API_KEY", "AK_ENV")
    monkeypatch.setenv("ALPACA_EQUITY_SECRET_KEY", "SK_ENV")
    out = resolve("equity")
    assert out.api_key == "AK_ENV"
    assert out.source == "env_bootstrap"


def test_resolve_returns_env_bootstrap_when_db_unreachable(
    clean_env, monkeypatch,
):
    """If MySQL is down, resolver still serves from env."""
    def _raises():
        raise RuntimeError("db down")
    monkeypatch.setattr(creds_mod, "_get_store", _raises)
    monkeypatch.setenv("ALPACA_EQUITY_API_KEY", "AK_ENV")
    monkeypatch.setenv("ALPACA_EQUITY_SECRET_KEY", "SK_ENV")
    out = resolve("equity")
    assert out.api_key == "AK_ENV"


# ---------------------------------------------------------------------------
# test_connection tests
# ---------------------------------------------------------------------------

from unittest.mock import MagicMock

# Aliased on import to keep pytest from collecting it as a test function
# (its name starts with `test_` and it takes a positional arg pytest reads as a fixture).
from broker.credentials import test_connection as _test_connection_fn


def _creds(base="https://paper-api.alpaca.markets") -> AlpacaCreds:
    return AlpacaCreds(
        asset_class="equity", api_key="K", secret_key="S",
        base_url=base, source="db",
    )


def test_test_connection_200_returns_account_number(monkeypatch):
    resp = MagicMock(status_code=200)
    resp.json.return_value = {"account_number": "ABC1234", "status": "ACTIVE"}
    monkeypatch.setattr("broker.credentials.requests.get", lambda *a, **kw: resp)
    ok, msg = _test_connection_fn(_creds())
    assert ok is True
    assert msg == "ABC1234"


def test_test_connection_401_returns_invalid(monkeypatch):
    resp = MagicMock(status_code=401, text="unauthorized")
    monkeypatch.setattr("broker.credentials.requests.get", lambda *a, **kw: resp)
    ok, msg = _test_connection_fn(_creds())
    assert ok is False
    assert "Invalid" in msg


def test_test_connection_timeout(monkeypatch):
    def _raise(*a, **kw):
        raise requests.Timeout()
    import requests as _rq
    monkeypatch.setattr("broker.credentials.requests.get", _raise)
    ok, msg = _test_connection_fn(_creds())
    assert ok is False
    assert "timed out" in msg


def test_test_connection_inactive_account_warns(monkeypatch):
    resp = MagicMock(status_code=200)
    resp.json.return_value = {"account_number": "X", "status": "INACTIVE"}
    monkeypatch.setattr("broker.credentials.requests.get", lambda *a, **kw: resp)
    ok, msg = _test_connection_fn(_creds())
    assert ok is True
    assert "INACTIVE" in msg
