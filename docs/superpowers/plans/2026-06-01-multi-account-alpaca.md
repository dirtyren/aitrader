# Per-Asset-Class Alpaca Accounts + Dashboard Split — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Split the single shared Alpaca account into independent equity and crypto accounts with DB-backed credentials, dashboard editing with required test-connection, and a two-sub-tab Strategies view with colorized P&L.

**Architecture:** A new `broker/credentials.py` resolver is the single source of truth for credentials. It reads from a new `broker_credentials` MySQL table first, falls back to per-asset-class env vars (and seeds the DB on first hit), and finally falls back to the legacy single env var with a deprecation warning. `AlpacaClient` accepts an optional `asset_class` kwarg and delegates to the resolver. The Strategies tab renders two Streamlit sub-tabs and a new Settings tab manages credentials with a required test-connection step before save.

**Tech Stack:** Python 3, SQLAlchemy + MySQL, Streamlit, requests, pytest. Reuses existing `format_pnl` helper and `pnl-pos`/`pnl-neg`/`pnl-neu` CSS classes from `ui/components/theme.py`.

**Reference:** [`docs/superpowers/specs/2026-06-01-multi-account-alpaca-design.md`](../specs/2026-06-01-multi-account-alpaca-design.md)

---

## File Structure

**Create:**
- `broker/credentials.py` — resolver, `AlpacaCreds` dataclass, `resolve`/`upsert`/`test_connection` functions, `MissingCredentialsError`.
- `tests/test_credentials.py` — unit tests for resolver precedence and write-path.
- `tests/test_schema_broker_credentials.py` — schema migration test.
- `ui/tabs/settings_tab.py` — new dashboard tab for credential editing.
- `tests/test_settings_tab.py` — settings tab logic tests.
- `tests/test_strategies_tab_split.py` — `list_by_asset_class` + colorized helper tests.

**Modify:**
- `state/schema.sql` — add `broker_credentials` table.
- `state/mysql_store.py` — add `broker_credentials` migration to `ensure_schema`; add `BrokerCredentialsRow` ORM model; add credential CRUD methods.
- `broker/alpaca_client.py` — accept `asset_class` kwarg; delegate to resolver when present.
- `main.py` — pass asset class to `AlpacaClient`.
- `main_gap_and_go.py` — pass asset class to `AlpacaClient`.
- `ui/tabs/strategies_tab.py` — split into Equity / Crypto sub-tabs; per-asset-class `_get_alpaca`; colorized P&L cells.
- `ui/components/strategy_card.py` — colorized card P&L (already uses `format_pnl`; verify rendering).
- `ui/data/strategy_configs.py` — add `list_by_asset_class`.
- `ui/dashboard.py` — register new Settings tab.
- `config/.env.example` — document `ALPACA_{EQUITY,CRYPTO}_*` variables.
- `tests/test_alpaca_client_orders.py` and `tests/test_alpaca_client_bars.py` — add `asset_class=` construction tests.

---

## Task 1: Database schema for `broker_credentials`

**Files:**
- Modify: `state/schema.sql`
- Modify: `state/mysql_store.py:30-43,234-298`
- Test: `tests/test_schema_broker_credentials.py`

- [ ] **Step 1: Write the failing schema test**

Create `tests/test_schema_broker_credentials.py`:

```python
"""Schema test for broker_credentials table."""
from __future__ import annotations

import os
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine, text

from state.mysql_store import MySQLStore


@pytest.fixture
def store(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("MYSQL_HOST", "")  # force fallback
    # We'll use sqlite for unit tests since the migration is structural.
    # Patch _build_url to return a sqlite URL.
    from state import mysql_store as ms

    def _sqlite_url() -> str:
        return f"sqlite:///{db_path}"

    monkeypatch.setattr(ms, "_build_url", _sqlite_url)
    s = MySQLStore(strategy_name="test")
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_schema_broker_credentials.py -v`
Expected: FAIL — `broker_credentials` table does not exist.

- [ ] **Step 3: Add `BrokerCredentialsRow` ORM model**

In `state/mysql_store.py`, after `EventRow` (around line 188), add:

```python
class BrokerCredentialsRow(Base):
    __tablename__ = "broker_credentials"

    asset_class:    Mapped[str]      = mapped_column(String(16), primary_key=True)
    api_key:        Mapped[str]      = mapped_column(String(255), nullable=False)
    secret_key:     Mapped[str]      = mapped_column(String(255), nullable=False)
    base_url:       Mapped[str]      = mapped_column(String(255), nullable=False)
    account_number: Mapped[str | None] = mapped_column(String(64), nullable=True)
    updated_at:     Mapped[datetime] = mapped_column(DateTime, nullable=False)
```

Confirm `DateTime` and `String` are already imported at the top of the file (they are — they're used by other rows).

- [ ] **Step 4: Add the SQL definition to `state/schema.sql`**

Append this block to `state/schema.sql` (anywhere after the existing tables; matches the patterns used elsewhere):

```sql
CREATE TABLE IF NOT EXISTS broker_credentials (
    asset_class    VARCHAR(16) NOT NULL,
    api_key        VARCHAR(255) NOT NULL,
    secret_key     VARCHAR(255) NOT NULL,
    base_url       VARCHAR(255) NOT NULL,
    account_number VARCHAR(64) DEFAULT NULL,
    updated_at     DATETIME NOT NULL,
    PRIMARY KEY (asset_class)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/test_schema_broker_credentials.py -v`
Expected: PASS — `Base.metadata.create_all` now creates the table.

- [ ] **Step 6: Commit**

```bash
git add state/schema.sql state/mysql_store.py tests/test_schema_broker_credentials.py
git commit -m "feat(schema): add broker_credentials table for per-asset-class Alpaca creds"
```

---

## Task 2: `MySQLStore` CRUD for `broker_credentials`

**Files:**
- Modify: `state/mysql_store.py` (append new methods at end of class, before final blank line)
- Test: `tests/test_credentials.py` (we'll create the file in this task; it's empty for now)

- [ ] **Step 1: Write the failing CRUD test**

Create `tests/test_credentials.py`:

```python
"""Tests for broker credentials resolver and MySQLStore CRUD."""
from __future__ import annotations

from datetime import datetime

import pytest

from state import mysql_store as ms
from state.mysql_store import MySQLStore


@pytest.fixture
def store(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"

    def _sqlite_url() -> str:
        return f"sqlite:///{db_path}"

    monkeypatch.setattr(ms, "_build_url", _sqlite_url)
    s = MySQLStore(strategy_name="test")
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_credentials.py -v`
Expected: FAIL — `MySQLStore` has no `get_broker_credentials` / `upsert_broker_credentials` / `set_broker_credentials_account_number`.

- [ ] **Step 3: Implement the three CRUD methods**

In `state/mysql_store.py`, append to the `MySQLStore` class (preserve indentation; same pattern as existing methods):

```python
    # ------------------------------------------------------------------
    # broker_credentials CRUD
    # ------------------------------------------------------------------

    def get_broker_credentials(self, asset_class: str) -> dict | None:
        """Return a dict with keys api_key, secret_key, base_url, account_number,
        updated_at — or None if the row is missing or has empty key/secret."""
        with Session(self._engine) as sess:
            row = sess.get(BrokerCredentialsRow, asset_class)
            if row is None:
                return None
            if not row.api_key or not row.secret_key:
                return None
            return {
                "asset_class": row.asset_class,
                "api_key": row.api_key,
                "secret_key": row.secret_key,
                "base_url": row.base_url,
                "account_number": row.account_number,
                "updated_at": row.updated_at,
            }

    def upsert_broker_credentials(
        self,
        asset_class: str,
        api_key: str,
        secret_key: str,
        base_url: str,
    ) -> None:
        """Insert or update credentials for the asset class. Resets account_number
        to NULL — caller should re-test the connection and set it via
        set_broker_credentials_account_number."""
        now = datetime.utcnow()
        with Session(self._engine) as sess:
            row = sess.get(BrokerCredentialsRow, asset_class)
            if row is None:
                row = BrokerCredentialsRow(
                    asset_class=asset_class,
                    api_key=api_key,
                    secret_key=secret_key,
                    base_url=base_url,
                    account_number=None,
                    updated_at=now,
                )
                sess.add(row)
            else:
                row.api_key = api_key
                row.secret_key = secret_key
                row.base_url = base_url
                row.account_number = None
                row.updated_at = now
            sess.commit()

    def set_broker_credentials_account_number(
        self, asset_class: str, account_number: str,
    ) -> None:
        """Cache the Alpaca account number after a successful test_connection."""
        with Session(self._engine) as sess:
            row = sess.get(BrokerCredentialsRow, asset_class)
            if row is None:
                return
            row.account_number = account_number
            row.updated_at = datetime.utcnow()
            sess.commit()
```

Confirm `datetime` is imported at the top of the file (it is).

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_credentials.py -v`
Expected: PASS — all four tests green.

- [ ] **Step 5: Run full test suite to confirm no regressions**

Run: `pytest -x -q`
Expected: PASS, same count as before plus the new tests.

- [ ] **Step 6: Commit**

```bash
git add state/mysql_store.py tests/test_credentials.py
git commit -m "feat(state): broker_credentials CRUD on MySQLStore"
```

---

## Task 3: `broker/credentials.py` resolver

**Files:**
- Create: `broker/credentials.py`
- Modify: `tests/test_credentials.py` (extend with resolver tests)

- [ ] **Step 1: Write the failing resolver tests**

Append to `tests/test_credentials.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_credentials.py -v -k "resolve or invalid"`
Expected: FAIL with `ImportError` — `broker.credentials` does not exist.

- [ ] **Step 3: Implement the resolver**

Create `broker/credentials.py`:

```python
"""Per-asset-class Alpaca credential resolver.

Single source of truth for which API key / secret / base_url an AlpacaClient
uses. Lookup precedence:

    1. broker_credentials row in MySQL (with non-empty api_key & secret_key)
    2. ALPACA_{EQUITY,CRYPTO}_API_KEY / _SECRET_KEY / _BASE_URL env vars
       — on hit, the row is upserted into MySQL (the bootstrap)
    3. Legacy ALPACA_API_KEY / ALPACA_SECRET_KEY / ALPACA_BASE_URL — used
       for both asset classes with a one-time deprecation warning. Does not
       seed the DB.

If MySQL is unreachable the resolver logs and falls through to env-only — a
DB outage does not stop a trader that has its creds in .env.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Literal

import requests
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

AssetClass = Literal["equity", "crypto"]
_VALID_ASSET_CLASSES = ("equity", "crypto")
_DEFAULT_BASE_URL = "https://paper-api.alpaca.markets"

_LEGACY_WARN_LOGGED = False


class MissingCredentialsError(Exception):
    """Raised when no Alpaca credentials are configured for an asset class."""


@dataclass(frozen=True)
class AlpacaCreds:
    asset_class: AssetClass
    api_key: str
    secret_key: str
    base_url: str
    source: Literal["db", "env_bootstrap", "env_legacy"]


def _get_store():
    """Return a MySQLStore for credential reads/writes.

    Imported lazily so unit tests that patch this never spin up a DB.
    """
    from state.mysql_store import MySQLStore
    s = MySQLStore(strategy_name="credentials_resolver")
    s.ensure_schema()
    return s


def _validate(asset_class: str) -> AssetClass:
    if asset_class not in _VALID_ASSET_CLASSES:
        raise ValueError(
            f"Invalid asset_class {asset_class!r}; "
            f"expected one of {_VALID_ASSET_CLASSES}"
        )
    return asset_class  # type: ignore[return-value]


def _read_split_env(asset_class: AssetClass) -> tuple[str, str, str] | None:
    prefix = f"ALPACA_{asset_class.upper()}"
    api_key = os.environ.get(f"{prefix}_API_KEY", "")
    secret = os.environ.get(f"{prefix}_SECRET_KEY", "")
    if not api_key or not secret:
        return None
    base_url = os.environ.get(f"{prefix}_BASE_URL", _DEFAULT_BASE_URL)
    return api_key, secret, base_url


def _read_legacy_env() -> tuple[str, str, str] | None:
    api_key = os.environ.get("ALPACA_API_KEY", "")
    secret = os.environ.get("ALPACA_SECRET_KEY", "")
    if not api_key or not secret:
        return None
    base_url = os.environ.get("ALPACA_BASE_URL", _DEFAULT_BASE_URL)
    return api_key, secret, base_url


def resolve(asset_class: str) -> AlpacaCreds:
    """Look up credentials for the given asset class. See module docstring
    for precedence rules. Raises MissingCredentialsError when nothing is
    configured."""
    global _LEGACY_WARN_LOGGED

    ac = _validate(asset_class)
    load_dotenv()

    # 1. DB
    store = None
    try:
        store = _get_store()
    except Exception as exc:
        logger.warning(
            "CREDENTIALS_DB_UNREACHABLE asset_class=%s err=%s — falling back to env",
            ac, exc,
        )

    if store is not None:
        try:
            row = store.get_broker_credentials(ac)
        except Exception as exc:
            logger.warning(
                "CREDENTIALS_DB_READ_FAILED asset_class=%s err=%s", ac, exc,
            )
            row = None
        if row is not None:
            return AlpacaCreds(
                asset_class=ac,
                api_key=row["api_key"],
                secret_key=row["secret_key"],
                base_url=row["base_url"],
                source="db",
            )

    # 2. Split env vars
    split = _read_split_env(ac)
    if split is not None:
        api_key, secret, base_url = split
        if store is not None:
            try:
                store.upsert_broker_credentials(ac, api_key, secret, base_url)
            except Exception as exc:
                logger.warning(
                    "CREDENTIALS_DB_SEED_FAILED asset_class=%s err=%s", ac, exc,
                )
        return AlpacaCreds(
            asset_class=ac,
            api_key=api_key,
            secret_key=secret,
            base_url=base_url,
            source="env_bootstrap",
        )

    # 3. Legacy env vars
    legacy = _read_legacy_env()
    if legacy is not None:
        if not _LEGACY_WARN_LOGGED:
            logger.warning(
                "Using legacy ALPACA_API_KEY for both asset classes; "
                "set ALPACA_EQUITY_API_KEY / ALPACA_CRYPTO_API_KEY in .env "
                "or via dashboard to split."
            )
            _LEGACY_WARN_LOGGED = True
        api_key, secret, base_url = legacy
        return AlpacaCreds(
            asset_class=ac,
            api_key=api_key,
            secret_key=secret,
            base_url=base_url,
            source="env_legacy",
        )

    raise MissingCredentialsError(
        f"No Alpaca credentials configured for asset_class={ac!r}. "
        f"Set ALPACA_{ac.upper()}_API_KEY / _SECRET_KEY in .env or "
        f"configure via the dashboard Settings tab."
    )


def upsert(
    asset_class: str,
    api_key: str,
    secret_key: str,
    base_url: str,
) -> None:
    """Write credentials to MySQL (dashboard write-path)."""
    ac = _validate(asset_class)
    store = _get_store()
    store.upsert_broker_credentials(ac, api_key, secret_key, base_url)


def test_connection(creds: AlpacaCreds, *, timeout_s: float = 5.0) -> tuple[bool, str]:
    """Verify creds by hitting GET /v2/account.

    Returns (True, account_number) on 200 OK.
    Returns (False, reason) on any error.
    """
    url = f"{creds.base_url.rstrip('/')}/v2/account"
    headers = {
        "APCA-API-KEY-ID": creds.api_key,
        "APCA-API-SECRET-KEY": creds.secret_key,
    }
    try:
        resp = requests.get(url, headers=headers, timeout=timeout_s)
    except requests.Timeout:
        return False, "Cannot reach Alpaca — request timed out"
    except requests.RequestException as exc:
        return False, f"Network error: {exc}"

    if resp.status_code == 401:
        return False, "Invalid API key or secret"
    if resp.status_code != 200:
        return False, f"Alpaca returned HTTP {resp.status_code}: {resp.text[:200]}"

    try:
        body = resp.json()
    except ValueError:
        return False, "Alpaca response was not valid JSON"
    account_number = body.get("account_number") or ""
    status = body.get("status") or ""
    if status and status != "ACTIVE":
        # Caller decides whether to allow non-ACTIVE saves; we still report success
        # but signal in the message.
        return True, f"{account_number} (warning: status={status})"
    return True, account_number
```

- [ ] **Step 4: Run resolver tests to verify they pass**

Run: `pytest tests/test_credentials.py -v`
Expected: PASS — all CRUD + resolver tests green.

- [ ] **Step 5: Add `test_connection` tests**

Append to `tests/test_credentials.py`:

```python
# ---------------------------------------------------------------------------
# test_connection tests
# ---------------------------------------------------------------------------

from unittest.mock import MagicMock

from broker.credentials import test_connection


def _creds(base="https://paper-api.alpaca.markets") -> AlpacaCreds:
    return AlpacaCreds(
        asset_class="equity", api_key="K", secret_key="S",
        base_url=base, source="db",
    )


def test_test_connection_200_returns_account_number(monkeypatch):
    resp = MagicMock(status_code=200)
    resp.json.return_value = {"account_number": "ABC1234", "status": "ACTIVE"}
    monkeypatch.setattr("broker.credentials.requests.get", lambda *a, **kw: resp)
    ok, msg = test_connection(_creds())
    assert ok is True
    assert msg == "ABC1234"


def test_test_connection_401_returns_invalid(monkeypatch):
    resp = MagicMock(status_code=401, text="unauthorized")
    monkeypatch.setattr("broker.credentials.requests.get", lambda *a, **kw: resp)
    ok, msg = test_connection(_creds())
    assert ok is False
    assert "Invalid" in msg


def test_test_connection_timeout(monkeypatch):
    def _raise(*a, **kw):
        raise requests.Timeout()
    import requests as _rq
    monkeypatch.setattr("broker.credentials.requests.get", _raise)
    ok, msg = test_connection(_creds())
    assert ok is False
    assert "timed out" in msg


def test_test_connection_inactive_account_warns(monkeypatch):
    resp = MagicMock(status_code=200)
    resp.json.return_value = {"account_number": "X", "status": "INACTIVE"}
    monkeypatch.setattr("broker.credentials.requests.get", lambda *a, **kw: resp)
    ok, msg = test_connection(_creds())
    assert ok is True
    assert "INACTIVE" in msg
```

Add `import requests` at the top of the test file if not already.

- [ ] **Step 6: Run all credentials tests**

Run: `pytest tests/test_credentials.py -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add broker/credentials.py tests/test_credentials.py
git commit -m "feat(broker): per-asset-class credential resolver with .env bootstrap"
```

---

## Task 4: Wire `AlpacaClient` to the resolver

**Files:**
- Modify: `broker/alpaca_client.py:84-92`
- Modify: `tests/test_alpaca_client_orders.py` (add 1-2 new tests)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_alpaca_client_orders.py`:

```python
def test_alpaca_client_uses_resolver_when_asset_class_given(monkeypatch):
    """When asset_class is passed, AlpacaClient pulls creds via the resolver
    (not directly from os.environ)."""
    from broker.alpaca_client import AlpacaClient
    from broker import credentials as creds_mod
    from broker.credentials import AlpacaCreds

    # Strip env so we'd fail without the resolver path.
    for k in [
        "ALPACA_API_KEY", "ALPACA_SECRET_KEY", "ALPACA_BASE_URL",
        "ALPACA_EQUITY_API_KEY", "ALPACA_EQUITY_SECRET_KEY", "ALPACA_EQUITY_BASE_URL",
    ]:
        monkeypatch.delenv(k, raising=False)

    monkeypatch.setattr(creds_mod, "resolve", lambda ac: AlpacaCreds(
        asset_class="equity", api_key="AK", secret_key="SK",
        base_url="https://paper-api.alpaca.markets", source="db",
    ))

    client = AlpacaClient(asset_class="equity")
    assert client.api_key == "AK"
    assert client.secret_key == "SK"
    assert client.base_url == "https://paper-api.alpaca.markets"
    assert client.asset_class == "equity"


def test_alpaca_client_default_construction_still_uses_env(monkeypatch):
    """Backwards compat: AlpacaClient() with no asset_class reads env directly."""
    from broker.alpaca_client import AlpacaClient

    monkeypatch.setenv("ALPACA_API_KEY", "AK_ENV")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "SK_ENV")
    monkeypatch.delenv("ALPACA_BASE_URL", raising=False)

    client = AlpacaClient()
    assert client.api_key == "AK_ENV"
    assert client.secret_key == "SK_ENV"
    assert client.base_url == "https://paper-api.alpaca.markets"
    assert client.asset_class is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_alpaca_client_orders.py::test_alpaca_client_uses_resolver_when_asset_class_given -v`
Expected: FAIL — `AlpacaClient.__init__` does not accept `asset_class`.

- [ ] **Step 3: Modify `AlpacaClient.__init__`**

In `broker/alpaca_client.py`, replace lines 84–92 (the entire `__init__` body excluding the docstring) with:

```python
    def __init__(self, asset_class: str | None = None):
        load_dotenv()
        if asset_class is not None:
            from broker import credentials  # local import to avoid cycles
            creds = credentials.resolve(asset_class)
            self.api_key = creds.api_key
            self.secret_key = creds.secret_key
            self.base_url = creds.base_url.rstrip("/")
            self.asset_class = asset_class
        else:
            self.api_key = os.environ["ALPACA_API_KEY"]
            self.secret_key = os.environ["ALPACA_SECRET_KEY"]
            self.base_url = os.environ.get(
                "ALPACA_BASE_URL", "https://paper-api.alpaca.markets"
            ).rstrip("/")
            self.asset_class = None
        self._session = requests.Session()
        self._session.headers.update(self._get_headers())
```

Also update the class docstring (around lines 73–80) to mention the new arg:

```python
    """
    Thin HTTP wrapper around Alpaca Markets REST API v2.

    When constructed with `asset_class="equity"` or `asset_class="crypto"`,
    credentials are resolved via broker.credentials (DB → split env → legacy
    env). When constructed with no argument, falls back to the legacy
    env-only path:
        ALPACA_API_KEY    — required
        ALPACA_SECRET_KEY — required
        ALPACA_BASE_URL   — optional, defaults to paper trading endpoint
    """
```

- [ ] **Step 4: Run new tests to verify they pass**

Run: `pytest tests/test_alpaca_client_orders.py -v -k "resolver or default_construction"`
Expected: PASS.

- [ ] **Step 5: Run all alpaca client tests for regressions**

Run: `pytest tests/test_alpaca_client_orders.py tests/test_alpaca_client_bars.py tests/test_alpaca_data.py -v`
Expected: PASS — all existing tests keep working since the env-only path is unchanged.

- [ ] **Step 6: Commit**

```bash
git add broker/alpaca_client.py tests/test_alpaca_client_orders.py
git commit -m "feat(broker): AlpacaClient(asset_class=...) routes through credentials resolver"
```

---

## Task 5: Pass asset class from `main.py` and `main_gap_and_go.py`

**Files:**
- Modify: `main.py:328`
- Modify: `main_gap_and_go.py:140`
- Test: `tests/test_main_overrides.py` (smoke test of asset-class extraction)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_main_overrides.py` (or create a new test if the file structure doesn't fit):

```python
def test_main_extracts_asset_class_from_yaml_config():
    """The single-key asset_classes block is the source of truth for which
    Alpaca account a trader uses."""
    cfg_equity = {"asset_classes": {"equity": {"symbols": ["SPY"]}}}
    cfg_crypto = {"asset_classes": {"crypto": {"symbols": ["BTC/USD"]}}}

    assert next(iter(cfg_equity["asset_classes"].keys())) == "equity"
    assert next(iter(cfg_crypto["asset_classes"].keys())) == "crypto"


def test_main_yaml_with_multiple_asset_classes_picks_first(caplog):
    """Defensive: a misconfigured YAML with both keys still produces a deterministic
    choice (Python 3.7+ dict ordering)."""
    cfg = {"asset_classes": {"equity": {}, "crypto": {}}}
    assert next(iter(cfg["asset_classes"].keys())) == "equity"
```

This is a unit-level guard rather than a true main entry test (those need a full bootstrap). The integration check happens in manual verification.

- [ ] **Step 2: Run test to verify behavior**

Run: `pytest tests/test_main_overrides.py -v -k "asset_class"`
Expected: PASS (both new tests).

- [ ] **Step 3: Modify `main.py`**

In `main.py`, replace line 328:

```python
    alpaca = AlpacaClient()
```

with:

```python
    asset_class = next(iter(cfg["asset_classes"].keys()))
    alpaca = AlpacaClient(asset_class=asset_class)
```

- [ ] **Step 4: Modify `main_gap_and_go.py`**

In `main_gap_and_go.py`, replace line 140:

```python
    alpaca = AlpacaClient()
```

with:

```python
    asset_class = next(iter(cfg["asset_classes"].keys()))
    alpaca = AlpacaClient(asset_class=asset_class)
```

(Confirm `cfg` exists at line 140 — if the variable is named differently, use that name. Inspect the surrounding ~10 lines first if uncertain.)

- [ ] **Step 5: Run full test suite**

Run: `pytest -x -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add main.py main_gap_and_go.py tests/test_main_overrides.py
git commit -m "feat(main): pass asset_class to AlpacaClient at trader startup"
```

---

## Task 6: `list_by_asset_class` helper

**Files:**
- Modify: `ui/data/strategy_configs.py` (append helper after `list_yaml_strategy_names`)
- Test: `tests/test_strategies_tab_split.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_strategies_tab_split.py`:

```python
"""Tests for the equity/crypto strategies tab split helpers."""
from __future__ import annotations

from pathlib import Path

import pytest

from ui.data.strategy_configs import list_by_asset_class


@pytest.fixture
def yaml_dir(tmp_path) -> Path:
    """Three minimal YAMLs: one equity, one crypto, one with both (defensive)."""
    (tmp_path / "settings_orb_equity.yaml").write_text(
        "system:\n"
        "  name: orb_equity\n"
        "asset_classes:\n"
        "  equity:\n"
        "    symbols: [SPY]\n"
    )
    (tmp_path / "settings_rsi_crypto.yaml").write_text(
        "system:\n"
        "  name: rsi_crypto\n"
        "asset_classes:\n"
        "  crypto:\n"
        "    symbols: [BTC/USD]\n"
    )
    (tmp_path / "settings_mixed.yaml").write_text(
        "system:\n"
        "  name: mixed\n"
        "asset_classes:\n"
        "  equity:\n"
        "    symbols: [SPY]\n"
        "  crypto:\n"
        "    symbols: [BTC/USD]\n"
    )
    return tmp_path


def test_list_by_asset_class_equity_only(yaml_dir):
    names = list_by_asset_class("equity", config_dir=yaml_dir)
    assert "orb_equity" in names
    assert "rsi_crypto" not in names


def test_list_by_asset_class_crypto_only(yaml_dir):
    names = list_by_asset_class("crypto", config_dir=yaml_dir)
    assert "rsi_crypto" in names
    assert "orb_equity" not in names


def test_list_by_asset_class_includes_mixed_in_both(yaml_dir, caplog):
    eq = list_by_asset_class("equity", config_dir=yaml_dir)
    cr = list_by_asset_class("crypto", config_dir=yaml_dir)
    assert "mixed" in eq
    assert "mixed" in cr


def test_list_by_asset_class_invalid_raises(yaml_dir):
    with pytest.raises(ValueError):
        list_by_asset_class("options", config_dir=yaml_dir)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_strategies_tab_split.py -v -k "list_by_asset_class"`
Expected: FAIL — `list_by_asset_class` does not exist.

- [ ] **Step 3: Implement the helper**

In `ui/data/strategy_configs.py`, after `list_yaml_strategy_names` (around line 167), add:

```python
def list_by_asset_class(
    asset_class: str,
    config_dir: Path = Path("config"),
) -> list[str]:
    """Sorted strategy names whose YAML's `asset_classes` block contains the
    given key. A YAML with both keys appears in both lists with a warning."""
    if asset_class not in ("equity", "crypto"):
        raise ValueError(
            f"Invalid asset_class {asset_class!r}; expected 'equity' or 'crypto'"
        )
    out: list[str] = []
    for name, cfg in load_yaml_configs(config_dir).items():
        ac_names = {ac.name for ac in cfg.asset_classes}
        if asset_class in ac_names:
            if len(ac_names) > 1:
                logger.warning(
                    "STRATEGY_MIXED_ASSET_CLASSES name=%s classes=%s — "
                    "appears in both Equity and Crypto tabs",
                    name, sorted(ac_names),
                )
            out.append(name)
    return sorted(out)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_strategies_tab_split.py -v -k "list_by_asset_class"`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add ui/data/strategy_configs.py tests/test_strategies_tab_split.py
git commit -m "feat(ui): list_by_asset_class helper for Strategies tab split"
```

---

## Task 7: P&L colorize helper

**Files:**
- Modify: `ui/components/kpi_row.py` (add a code-span variant of `format_pnl`)
- Modify: `tests/test_strategies_tab_split.py` (extend with helper tests)

The existing `format_pnl` in `ui/components/kpi_row.py:7` uses CSS classes `pnl-pos` / `pnl-neg` / `pnl-neu`. We add a sibling helper `format_pnl_inline` that wraps numeric values for use inside the admin table cells (which currently render via markdown code-spans). The card already calls `format_pnl`, so it gets coloring for free once we verify the theme CSS is loaded on the Strategies page.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_strategies_tab_split.py`:

```python
# ---------------------------------------------------------------------------
# format_pnl_inline tests (P&L colorization for admin table cells)
# ---------------------------------------------------------------------------

from ui.components.kpi_row import format_pnl_inline


def test_format_pnl_inline_positive_uses_pos_class():
    out = format_pnl_inline(5.0, fmt="{:+.2f}")
    assert "pnl-pos" in out
    assert "+5.00" in out


def test_format_pnl_inline_negative_uses_neg_class():
    out = format_pnl_inline(-3.5, fmt="{:+.2f}")
    assert "pnl-neg" in out
    assert "-3.50" in out


def test_format_pnl_inline_zero_treated_as_neutral():
    out = format_pnl_inline(0.0, fmt="{:+.2f}")
    assert "pnl-neu" in out


def test_format_pnl_inline_none_returns_em_dash_neutral():
    out = format_pnl_inline(None)
    assert "—" in out
    assert "pnl-neu" in out


def test_format_pnl_inline_nan_returns_em_dash_neutral():
    import math
    out = format_pnl_inline(math.nan)
    assert "—" in out
    assert "pnl-neu" in out
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_strategies_tab_split.py -v -k "format_pnl_inline"`
Expected: FAIL — `format_pnl_inline` does not exist.

- [ ] **Step 3: Implement the helper**

In `ui/components/kpi_row.py`, after the existing `format_pnl` (around line 18), add:

```python
def format_pnl_inline(value: Optional[float], *, fmt: str = "{:+.2f}") -> str:
    """Like `format_pnl` but renders the raw formatted number (no $ prefix)
    inside a colored monospace span. Use inside table cells where you want
    the value to look like a code-span but with red/green coloring.

    None or NaN renders as a neutral em-dash.
    Zero is treated as neutral (neither positive nor negative).
    """
    import math

    if value is None or (isinstance(value, float) and math.isnan(value)):
        return '<span class="pnl-neu" style="font-family:monospace">—</span>'
    if value > 0:
        cls = "pnl-pos"
    elif value < 0:
        cls = "pnl-neg"
    else:
        cls = "pnl-neu"
    return f'<span class="{cls}" style="font-family:monospace">{fmt.format(value)}</span>'
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_strategies_tab_split.py -v -k "format_pnl_inline"`
Expected: PASS — all five tests green.

- [ ] **Step 5: Commit**

```bash
git add ui/components/kpi_row.py tests/test_strategies_tab_split.py
git commit -m "feat(ui): format_pnl_inline helper for table-cell P&L colorization"
```

---

## Task 8: Strategies tab — split into Equity / Crypto sub-tabs with colorized P&L

**Files:**
- Modify: `ui/tabs/strategies_tab.py`

Read the file to confirm current structure before editing — it's the template that other parts of the dashboard mimic.

- [ ] **Step 1: Replace `_get_alpaca` and `render` with asset-class-aware versions**

In `ui/tabs/strategies_tab.py`, replace lines 20–22:

```python
@st.cache_resource
def _get_alpaca() -> AlpacaClient:
    return AlpacaClient()
```

with:

```python
@st.cache_resource
def _get_alpaca(asset_class: str) -> AlpacaClient:
    return AlpacaClient(asset_class=asset_class)
```

In the same file, replace the `render` function (lines 43–66) with:

```python
def render() -> None:
    start, end = period_selector.render()
    st.session_state["period"] = (start, end)

    selected: str | None = st.session_state.get("selected_strategy")
    if selected is not None:
        if st.button("← Back to all strategies"):
            st.session_state["selected_strategy"] = None
            st.rerun()
        _render_detail(selected, start, end)
        return

    eq_tab, cr_tab = st.tabs(["Equity", "Crypto"])
    with eq_tab:
        _render_asset_class("equity", start, end)
    with cr_tab:
        _render_asset_class("crypto", start, end)
```

- [ ] **Step 2: Add `_render_asset_class` helper**

In the same file, after `render`, add:

```python
def _mask_account_number(account_number: str | None) -> str:
    if not account_number:
        return "—"
    if len(account_number) <= 4:
        return f"{account_number}***"
    return f"{account_number[:4]}***"


def _render_asset_class(asset_class: str, start: datetime, end: datetime) -> None:
    """Header + admin table + cards, all filtered to one asset class."""
    from ui.data.strategy_configs import list_by_asset_class

    try:
        client = _get_alpaca(asset_class)
        account = client.get_account()
        acct_str = _mask_account_number(account.get("account_number"))
        bp = float(account.get("buying_power") or 0)
        st.caption(
            f"acct: `{acct_str}`  •  buying power: `${bp:,.0f}`  •  "
            f"asset class: **{asset_class}**"
        )
    except Exception as exc:
        st.warning(
            f"Could not load {asset_class} account info: {exc}. "
            f"Configure credentials in **Settings**."
        )

    try:
        names = list_by_asset_class(asset_class)
    except Exception as exc:
        st.error(f"Failed to load {asset_class} strategy configs: {exc}")
        return
    if not names:
        st.info(f"No {asset_class} strategies configured yet.")
        return

    _render_admin_panel(start, end, names)

    st.markdown("---")
    cols = st.columns(2)
    for i, name in enumerate(names):
        df = trades_repo.get_closed_trades(name, start, end)
        kpis = stats.compute_kpis(df)
        with cols[i % 2]:
            if strategy_card.render(name, kpis):
                st.session_state["selected_strategy"] = name
                st.rerun()
```

- [ ] **Step 3: Filter `_render_admin_panel` to a name list**

Replace the existing `_render_admin_panel(start, end)` signature and body (lines 97–190) with:

```python
def _render_admin_panel(
    start: datetime, end: datetime, allow_names: list[str],
) -> None:
    """Per-strategy kill-switch table, restricted to `allow_names`.

    Disable: confirms with a required operator note (≥3 chars), submits
    market closes for all open positions synchronously. On any failure
    the strategy stays in `disabling` and the trader retries on its loop.
    Re-enable: single click, no modal — re-enabling cannot lose money.

    P&L cells render via format_pnl_inline so negatives are red, positives green.
    """
    from ui.components.kpi_row import format_pnl_inline

    st.markdown("### Strategy controls")
    try:
        admin_store = _get_admin_store()
        df = strategy_admin.get_admin_view(admin_store, start, end)
    except Exception as exc:
        st.error(f"Could not load strategy admin view: {exc}")
        return
    if df.empty:
        st.info("No strategies registered yet.")
        return

    df = df[df["name"].isin(allow_names)]
    if df.empty:
        st.info("No strategies in this asset class.")
        return

    header = st.columns([2, 1, 1, 1, 1, 1, 1, 1, 1, 2])
    header[0].markdown("**Name**")
    header[1].markdown("**State**")
    header[2].markdown("**Open**")
    header[3].markdown("**Today P&L**")
    header[4].markdown("**P&L**")
    header[5].markdown("**Win rate**")
    header[6].markdown("**Sharpe**")
    header[7].markdown("**Max DD**")
    header[8].markdown("**Avg R**")
    header[9].markdown("**Action**")

    for _, row in df.iterrows():
        sid = int(row["id"])
        name = str(row["name"])
        state = str(row["state"])
        emoji, label = _STATE_BADGE.get(state, ("⚪", state))
        confirm_key = f"strat_confirm_{sid}"
        note_key = f"strat_note_{sid}"

        cols = st.columns([2, 1, 1, 1, 1, 1, 1, 1, 1, 2])
        cols[0].markdown(f"**{name}**")
        cols[1].markdown(f"{emoji} {label}")
        cols[2].markdown(f"`{int(row['open_count'])}`")
        cols[3].markdown(
            format_pnl_inline(row["today_pnl"], fmt="{:+.2f}"),
            unsafe_allow_html=True,
        )
        cols[4].markdown(
            format_pnl_inline(row["period_pnl"], fmt="{:+.2f}"),
            unsafe_allow_html=True,
        )
        cols[5].markdown(_fmt_code(format_pct(row["period_win_rate"])))
        cols[6].markdown(_fmt_code(format_num(row["period_sharpe"], fmt="{:.2f}")))
        cols[7].markdown(_fmt_code(format_num(row["period_max_dd"], fmt="{:+.0f}")))
        cols[8].markdown(
            format_pnl_inline(row["period_avg_r"], fmt="{:+.2f}R"),
            unsafe_allow_html=True,
        )

        with cols[9]:
            confirming = st.session_state.get(confirm_key, False)
            if state == "enabled":
                if not confirming:
                    if st.button("Disable", key=f"strat_btn_{sid}",
                                 type="primary"):
                        st.session_state[confirm_key] = True
                        st.rerun()
            elif state == "disabled":
                if st.button("Enable", key=f"strat_btn_{sid}"):
                    admin_store.set_strategy_state(
                        strategy_id=sid, enabled=True, state="enabled",
                        reason="operator_enable",
                    )
                    st.rerun()
            else:  # disabling
                st.caption(f"Sweeping… {int(row['open_count'])} left")

        if state == "enabled" and st.session_state.get(confirm_key):
            with st.container(border=True):
                st.warning(
                    f"Disabling **{name}** will submit market closes for "
                    f"**{int(row['open_count'])}** open position(s)."
                )
                note = st.text_input(
                    "Operator note (≥3 chars, required)",
                    key=note_key,
                    placeholder="e.g. risk review — pause until tomorrow",
                )
                c, x = st.columns([1, 1])
                disabled = len((note or "").strip()) < 3
                if c.button("Confirm disable", key=f"strat_go_{sid}",
                            type="primary", disabled=disabled):
                    _run_disable(sid, name, note)
                    st.session_state[confirm_key] = False
                    st.rerun()
                if x.button("Cancel", key=f"strat_x_{sid}"):
                    st.session_state[confirm_key] = False
                    st.rerun()
```

- [ ] **Step 4: Update `_run_disable` to use the per-asset-class client**

In the same file, replace the `_run_disable` function. It currently calls `_get_alpaca()` (no args) which is now invalid:

```python
def _run_disable(strategy_id: int, strategy_name: str, note: str) -> None:
    """Set state=disabling, sweep positions, transition to disabled on
    clean sweep — otherwise leave at disabling for the trader to retry."""
    from ui.data.strategy_configs import load_yaml_configs

    try:
        cfg = load_yaml_configs().get(strategy_name)
        if cfg is None or not cfg.asset_classes:
            st.error(f"No YAML config found for {strategy_name}; cannot derive "
                     "asset class for sweep.")
            return
        asset_class = cfg.asset_classes[0].name
    except Exception as exc:
        st.error(f"Failed to read config for {strategy_name}: {exc}")
        return

    try:
        admin_store = _get_admin_store()
        strategy_store = _get_store_for(strategy_name)
        admin_store.set_strategy_state(
            strategy_id=strategy_id,
            enabled=False, state="disabling",
            reason=note,
        )
    except Exception as exc:
        st.error(f"Could not mark strategy disabling: {exc}")
        return

    try:
        with st.spinner(f"Closing positions for {strategy_name}…"):
            result = close_all_open_positions(
                alpaca=_get_alpaca(asset_class), mysql=strategy_store,
                strategy_name=strategy_name, reason="operator_disable",
            )
    except Exception as exc:
        st.error(f"Sweep failed: {exc}. Strategy left in `disabling` "
                 "state — trader will retry on its next cycle.")
        return

    if not result.failed and result.total == len(result.closed):
        admin_store.set_strategy_state(
            strategy_id=strategy_id,
            enabled=False, state="disabled",
            reason="dashboard_disable_complete",
        )
        st.success(
            f"Disabled **{strategy_name}** — closed "
            f"{len(result.closed)}/{result.total} positions."
        )
    else:
        failed_summary = ", ".join(f"{s} ({why})" for s, why in result.failed)
        st.warning(
            f"Closed {len(result.closed)}/{result.total} positions. "
            f"Failures: {failed_summary or 'none'}. "
            "Strategy left in `disabling` state — trader will retry on "
            "its next cycle."
        )
```

- [ ] **Step 5: Delete the old `_render_landing` function**

The old `_render_landing` (lines 69–83) is now unreachable — `render` calls `_render_asset_class` directly. Delete it.

- [ ] **Step 6: Run smoke tests**

Run: `pytest tests/ -x -q -k "strategies or strategy or alpaca or credentials"`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add ui/tabs/strategies_tab.py
git commit -m "feat(dashboard): split Strategies tab into Equity/Crypto sub-tabs with colorized P&L"
```

---

## Task 9: Settings tab for credential editing

**Files:**
- Create: `ui/tabs/settings_tab.py`
- Modify: `ui/dashboard.py:21` (import + register)
- Test: `tests/test_settings_tab.py`

- [ ] **Step 1: Write the failing settings tab test**

Create `tests/test_settings_tab.py`:

```python
"""Tests for the dashboard Settings tab credential save flow."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from ui.tabs import settings_tab as st_mod


def test_save_calls_upsert_when_test_passes(monkeypatch):
    upsert_calls: list[tuple] = []
    monkeypatch.setattr(st_mod, "_upsert", lambda *a: upsert_calls.append(a))
    monkeypatch.setattr(
        st_mod, "_test_connection",
        lambda *a, **kw: (True, "ABC1234"),
    )
    monkeypatch.setattr(
        st_mod, "_set_account_number",
        lambda *a: None,
    )
    ok, msg = st_mod.save_credentials(
        asset_class="equity",
        api_key="AK", secret_key="SK",
        base_url="https://paper-api.alpaca.markets",
    )
    assert ok is True
    assert "ABC1234" in msg
    assert len(upsert_calls) == 1
    assert upsert_calls[0] == (
        "equity", "AK", "SK", "https://paper-api.alpaca.markets",
    )


def test_save_blocks_when_test_fails(monkeypatch):
    upsert_calls: list[tuple] = []
    monkeypatch.setattr(st_mod, "_upsert", lambda *a: upsert_calls.append(a))
    monkeypatch.setattr(
        st_mod, "_test_connection",
        lambda *a, **kw: (False, "Invalid API key or secret"),
    )
    ok, msg = st_mod.save_credentials(
        asset_class="equity",
        api_key="bad", secret_key="bad",
        base_url="https://paper-api.alpaca.markets",
    )
    assert ok is False
    assert "Invalid" in msg
    assert upsert_calls == []


def test_containers_for_asset_class_lists_equity_traders(tmp_path, monkeypatch):
    """The save banner lists the trader containers needing a restart."""
    (tmp_path / "settings_orb_equity.yaml").write_text(
        "system:\n  name: orb_equity\nasset_classes:\n  equity:\n    symbols: [SPY]\n"
    )
    (tmp_path / "settings_rsi_crypto.yaml").write_text(
        "system:\n  name: rsi_crypto\nasset_classes:\n  crypto:\n    symbols: [BTC/USD]\n"
    )
    out = st_mod.containers_for_asset_class("equity", config_dir=tmp_path)
    assert any("orb_equity" in c for c in out)
    assert not any("rsi_crypto" in c for c in out)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_settings_tab.py -v`
Expected: FAIL with `ImportError` — `ui.tabs.settings_tab` does not exist.

- [ ] **Step 3: Create the Settings tab module**

Create `ui/tabs/settings_tab.py`:

```python
"""Dashboard Settings tab — manage per-asset-class Alpaca credentials.

Credentials live in MySQL (broker_credentials table). This tab is the
write-path: an operator types a new key/secret, runs a required
test-connection step (GET /v2/account), and only then can save. Saved
changes require a trader-container restart to take effect; the success
banner lists which containers to restart.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Iterable

import streamlit as st

from broker import credentials as creds_mod
from broker.credentials import AlpacaCreds
from ui.data.strategy_configs import list_by_asset_class


# ---------------------------------------------------------------------------
# Pure-logic helpers (testable without Streamlit)
# ---------------------------------------------------------------------------

def _test_connection(api_key: str, secret_key: str, base_url: str) -> tuple[bool, str]:
    creds = AlpacaCreds(
        asset_class="equity",  # placeholder; resolver doesn't validate here
        api_key=api_key, secret_key=secret_key, base_url=base_url,
        source="db",
    )
    return creds_mod.test_connection(creds)


def _upsert(asset_class: str, api_key: str, secret_key: str, base_url: str) -> None:
    creds_mod.upsert(asset_class, api_key, secret_key, base_url)


def _set_account_number(asset_class: str, account_number: str) -> None:
    from state.mysql_store import MySQLStore
    s = MySQLStore(strategy_name="settings_tab")
    s.ensure_schema()
    s.set_broker_credentials_account_number(asset_class, account_number)


def save_credentials(
    asset_class: str,
    api_key: str,
    secret_key: str,
    base_url: str,
) -> tuple[bool, str]:
    """Test connection then persist on success.

    Returns (True, account_number_message) when the credentials are saved.
    Returns (False, error_reason) when the test fails — no DB write.
    """
    ok, msg = _test_connection(api_key, secret_key, base_url)
    if not ok:
        return False, msg
    _upsert(asset_class, api_key, secret_key, base_url)
    # Cache the account number from the successful test for the dashboard header.
    account_number = msg.split(" ", 1)[0]  # strip trailing "(warning: …)" if present
    if account_number:
        try:
            _set_account_number(asset_class, account_number)
        except Exception:
            pass  # best-effort; primary save already succeeded
    return True, msg


def containers_for_asset_class(
    asset_class: str,
    config_dir: Path = Path("config"),
) -> list[str]:
    """Trader container names that need a restart after a credential save.

    Naming convention from docker-compose.yml: trader-<strategy-name-with-dashes>.
    """
    names = list_by_asset_class(asset_class, config_dir=config_dir)
    return [f"trader-{name.replace('_', '-')}" for name in names]


def _mask_key(key: str | None) -> str:
    if not key:
        return "—"
    if len(key) <= 8:
        return key[0] + "***"
    return f"{key[:4]}***{key[-2:]}"


# ---------------------------------------------------------------------------
# Streamlit render
# ---------------------------------------------------------------------------

def render() -> None:
    st.subheader("Alpaca credentials")
    st.caption(
        "Per-asset-class API keys. Saving requires a successful test "
        "connection; trader containers must be restarted to apply."
    )
    eq, cr = st.columns(2)
    with eq:
        _render_card("equity")
    with cr:
        _render_card("crypto")


def _render_card(asset_class: str) -> None:
    with st.container(border=True):
        st.markdown(f"### {asset_class.capitalize()}")
        try:
            from state.mysql_store import MySQLStore
            store = MySQLStore(strategy_name="settings_tab_view")
            store.ensure_schema()
            row = store.get_broker_credentials(asset_class)
        except Exception as exc:
            st.error(f"DB error, cannot edit: {exc}")
            return

        if row is None:
            st.markdown("**Status:** _not configured_")
            current_base = "https://paper-api.alpaca.markets"
            current_account = ""
            updated_at: datetime | None = None
        else:
            st.markdown(
                f"**Status:** account `{row.get('account_number') or '—'}`  "
                f"key `{_mask_key(row['api_key'])}`"
            )
            current_base = row["base_url"]
            current_account = row.get("account_number") or ""
            updated_at = row.get("updated_at")

        if updated_at is not None:
            st.caption(f"Last updated: `{updated_at.isoformat(sep=' ', timespec='seconds')}`")

        edit_key = f"settings_edit_{asset_class}"
        editing = st.session_state.get(edit_key, False)

        if not editing:
            if st.button(f"Edit {asset_class}", key=f"settings_edit_btn_{asset_class}"):
                st.session_state[edit_key] = True
                st.rerun()
            return

        st.markdown("---")
        api_key = st.text_input(
            "API key", key=f"settings_api_{asset_class}", type="password",
        )
        secret_key = st.text_input(
            "Secret key", key=f"settings_secret_{asset_class}", type="password",
        )
        base_url = st.text_input(
            "Base URL", value=current_base, key=f"settings_base_{asset_class}",
        )

        test_state_key = f"settings_test_ok_{asset_class}"
        test_msg_key = f"settings_test_msg_{asset_class}"

        cc, ss, xx = st.columns([1, 1, 1])
        if cc.button("Test connection", key=f"settings_test_btn_{asset_class}"):
            ok, msg = _test_connection(api_key, secret_key, base_url)
            st.session_state[test_state_key] = ok
            st.session_state[test_msg_key] = msg

        save_disabled = not st.session_state.get(test_state_key, False) or not (
            api_key and secret_key and base_url
        )
        if ss.button(
            "Save", key=f"settings_save_btn_{asset_class}",
            type="primary", disabled=save_disabled,
        ):
            ok, msg = save_credentials(asset_class, api_key, secret_key, base_url)
            if ok:
                containers = containers_for_asset_class(asset_class)
                st.success(
                    f"Saved {asset_class} credentials (account {msg}).  "
                    f"**Restart these containers to apply:**  "
                    + ", ".join(f"`{c}`" for c in containers)
                )
                # Clear cached AlpacaClient so the dashboard picks up new creds.
                from ui.tabs.strategies_tab import _get_alpaca
                _get_alpaca.clear()
                st.session_state[edit_key] = False
                st.session_state[test_state_key] = False
                st.session_state[test_msg_key] = ""
                st.rerun()
            else:
                st.error(f"Save failed: {msg}")
        if xx.button("Cancel", key=f"settings_cancel_btn_{asset_class}"):
            st.session_state[edit_key] = False
            st.session_state[test_state_key] = False
            st.session_state[test_msg_key] = ""
            st.rerun()

        # Inline test result.
        msg = st.session_state.get(test_msg_key, "")
        if msg:
            if st.session_state.get(test_state_key):
                st.success(f"✓ Connection OK — {msg}")
            else:
                st.error(f"✗ {msg}")
```

- [ ] **Step 4: Register the Settings tab in `ui/dashboard.py`**

Read `ui/dashboard.py` first to see the tab-registration pattern. Then in the import block (line 21), add `settings_tab`:

```python
from ui.tabs import config_tab, live_tab, reconciliation_tab, settings_tab, strategies_tab
```

Find the `st.tabs([...])` call in `dashboard.py` and add a `"Settings"` entry, plus its `with` block calling `settings_tab.render()`. Position it after Config, before Logs (per spec). The exact pattern depends on the existing tab list — match the surrounding code's style.

- [ ] **Step 5: Run settings tab tests**

Run: `pytest tests/test_settings_tab.py -v`
Expected: PASS — three tests green.

- [ ] **Step 6: Run full test suite**

Run: `pytest -x -q`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add ui/tabs/settings_tab.py ui/dashboard.py tests/test_settings_tab.py
git commit -m "feat(dashboard): Settings tab for per-asset-class Alpaca credentials"
```

---

## Task 10: Document `.env` variables

**Files:**
- Modify: `config/.env.example`

- [ ] **Step 1: Replace the Alpaca section in `.env.example`**

Read `config/.env.example` to confirm current contents, then replace the Alpaca block (the three legacy lines) with:

```bash
# Per-asset-class Alpaca credentials.
# Set EITHER these (recommended) OR the legacy ALPACA_API_KEY/SECRET_KEY block
# below. With the legacy block both equity and crypto traders share one account
# and a deprecation warning is logged.

ALPACA_EQUITY_API_KEY=
ALPACA_EQUITY_SECRET_KEY=
ALPACA_EQUITY_BASE_URL=https://paper-api.alpaca.markets

ALPACA_CRYPTO_API_KEY=
ALPACA_CRYPTO_SECRET_KEY=
ALPACA_CRYPTO_BASE_URL=https://paper-api.alpaca.markets

# Legacy single-account variables (deprecated — kept for backwards compat).
# Used for both asset classes when the per-asset-class vars above are unset.
ALPACA_API_KEY=
ALPACA_SECRET_KEY=
ALPACA_BASE_URL=https://paper-api.alpaca.markets
```

Leave the rest of the file (DASH_USER, TRADING_ENV, TELEGRAM_*) unchanged.

- [ ] **Step 2: Commit**

```bash
git add config/.env.example
git commit -m "docs(env): document per-asset-class ALPACA_{EQUITY,CRYPTO}_* variables"
```

---

## Task 11: Final verification

- [ ] **Step 1: Run the full test suite**

Run: `pytest -q`
Expected: PASS, with no skips for the new files.

- [ ] **Step 2: Manual verification checklist (run before merging)**

1. Fresh DB + `.env` with `ALPACA_EQUITY_*` and `ALPACA_CRYPTO_*` set → start one equity + one crypto trader (`docker compose up trader-orb-equity trader-orb-crypto`). Check `broker_credentials` table has 2 rows; each trader log shows the correct account number from `account = alpaca.get_account()`.

2. Open dashboard → **Strategies** → **Equity** sub-tab shows only the 5 equity strategies (orb, rsi, vwap_bands, vwap_wave, gap_and_go) + equity account number in the header. **Crypto** sub-tab shows the 5 crypto strategies + crypto account number.

3. Trigger a closed trade with negative P&L on a paper account → admin table cell renders red (`pnl-neg`). Positive P&L renders green (`pnl-pos`). Same on the strategy cards.

4. **Settings** tab → **Equity** card → Edit → enter a wrong secret → Test connection → red error "Invalid API key or secret" → Save button disabled.

5. **Settings** tab → **Equity** card → Edit → enter a valid secret → Test connection → green "✓ Connection OK — ABC1234" → Save → green banner lists 5 trader containers to restart → restart one container (`docker compose restart trader-orb-equity`) → its log shows the new account number.

6. Restart with `.env` containing only legacy `ALPACA_API_KEY` (no per-asset-class vars) → both asset classes work; `WARNING: Using legacy ALPACA_API_KEY for both asset classes; …` appears once in the log.

- [ ] **Step 3: Done.** No further commits unless verification surfaces an issue.

---

## Self-Review Notes

**Spec coverage check:**
- Per-asset-class credentials: Tasks 1–5.
- DB-backed with `.env` bootstrap: Task 3 (resolver precedence).
- Test-connection required before save: Task 9 (`save_credentials` blocks on failure) + Task 3 (`test_connection` impl).
- Plaintext storage in restricted table: Task 1 (schema), no encryption layer added.
- Two-sub-tab strategies layout: Task 8.
- Per-asset-class buying-power header: Task 8 (`_render_asset_class`).
- Colorized P&L (Today, Period, Avg R, card): Task 7 (`format_pnl_inline`) + Task 8 (admin table) + existing `format_pnl` in `strategy_card.py`.
- Per-account `base_url`: Task 1 (column exists), Task 9 (form field), Task 3 (resolver carries it through).
- Restart-required banner: Task 9 (`containers_for_asset_class`).
- Legacy fallback with deprecation warning: Task 3 resolver tests.
- `MissingCredentialsError` fail-fast at trader startup: implicit in Task 3 (resolver raises) + Task 5 (main propagates the exception).

**Type consistency check:**
- `AlpacaCreds.asset_class` typed `Literal["equity", "crypto"]`; `resolve` returns the validated string (mypy-correct via `_validate`).
- `MySQLStore.get_broker_credentials` returns `dict | None`; `resolve` reads `row["api_key"]` etc — keys consistent with what `upsert_broker_credentials` writes.
- `save_credentials` returns `tuple[bool, str]`, matching `_test_connection`'s return type.
- `containers_for_asset_class` returns `list[str]`, used as iterable in the success banner — consistent.

**No placeholders:** Searched plan text for "TBD" / "implement later" / "add appropriate" — none found. Every task contains the actual code or exact command.
