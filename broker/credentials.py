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


def _mask_key(key: str) -> str:
    """Mask an API key for logging: first 4 chars + *** + last 2 chars.
    Short keys collapse to a single safe placeholder."""
    if not key or len(key) < 8:
        return "***"
    return f"{key[:4]}***{key[-2:]}"


def resolve(asset_class: str) -> AlpacaCreds:
    """Look up credentials for the given asset class. See module docstring
    for precedence rules. Raises MissingCredentialsError when nothing is
    configured."""
    global _LEGACY_WARN_LOGGED

    ac = _validate(asset_class)
    load_dotenv()
    creds: AlpacaCreds | None = None

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
            creds = AlpacaCreds(
                asset_class=ac,
                api_key=row["api_key"],
                secret_key=row["secret_key"],
                base_url=row["base_url"],
                source="db",
            )

    # 2. Split env vars
    if creds is None:
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
            creds = AlpacaCreds(
                asset_class=ac,
                api_key=api_key,
                secret_key=secret,
                base_url=base_url,
                source="env_bootstrap",
            )

    # 3. Legacy env vars
    if creds is None:
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
            creds = AlpacaCreds(
                asset_class=ac,
                api_key=api_key,
                secret_key=secret,
                base_url=base_url,
                source="env_legacy",
            )

    if creds is None:
        raise MissingCredentialsError(
            f"No Alpaca credentials configured for asset_class={ac!r}. "
            f"Set ALPACA_{ac.upper()}_API_KEY / _SECRET_KEY in .env or "
            f"configure via the dashboard Settings tab."
        )

    logger.info(
        "CREDENTIALS_RESOLVED asset_class=%s source=%s key=%s base_url=%s",
        creds.asset_class, creds.source, _mask_key(creds.api_key), creds.base_url,
    )
    return creds


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
