"""client_order_id (COID) — strategy-attribution stamp on every order.

Format: aitrader__<strategy>__<setup>__<symbol>__<role>__<uuid8>

Pure functions; no I/O. The single source of truth for the format used by
OrderExecutor (writers) and the future reconciler service (readers).
"""
from __future__ import annotations

import re
import uuid
from typing import Final

PREFIX: Final[str] = "aitrader"
SEPARATOR: Final[str] = "__"
MAX_LENGTH: Final[int] = 128

_STRATEGY_MAX = 32
_SETUP_MAX = 32
_SYMBOL_MAX = 16
_ROLE_MAX = 7
_UUID_LEN = 8

_RE_STRATEGY_SETUP = re.compile(r"[^a-z0-9_]")
_RE_SYMBOL = re.compile(r"[^A-Z0-9]")
_RE_UUID = re.compile(r"^[0-9a-f]{8}$")


class Role:
    """COID role values. Plain string constants — no enum machinery needed."""
    ENTRY = "entry"
    EXIT = "exit"
    STOP = "stop"
    TARGET = "target"
    ADOPTED = "adopted"


_VALID_ROLES = frozenset({Role.ENTRY, Role.EXIT, Role.STOP, Role.TARGET, Role.ADOPTED})


def _sanitize_strategy_or_setup(value: str, max_len: int) -> str:
    cleaned = _RE_STRATEGY_SETUP.sub("_", value.lower())
    # Collapse runs of "_" so they cannot collide with the "__" separator,
    # then strip leading/trailing "_" so segments never start or end with one.
    cleaned = re.sub(r"_+", "_", cleaned).strip("_")
    return cleaned[:max_len]


def _sanitize_symbol(value: str) -> str:
    # Strip slashes first, then uppercase, then drop anything not [A-Z0-9].
    cleaned = _RE_SYMBOL.sub("", value.replace("/", "").upper())
    return cleaned[:_SYMBOL_MAX]


def make_client_order_id(strategy: str, setup: str, symbol: str, role: str) -> str:
    """Build a COID from its components. Raises ValueError on invalid input."""
    if not strategy or not setup or not symbol:
        raise ValueError(
            f"client_order_id requires non-empty strategy/setup/symbol "
            f"(got strategy={strategy!r} setup={setup!r} symbol={symbol!r})"
        )
    if role not in _VALID_ROLES:
        raise ValueError(
            f"client_order_id role={role!r} not in {sorted(_VALID_ROLES)}"
        )

    s_strategy = _sanitize_strategy_or_setup(strategy, _STRATEGY_MAX)
    s_setup = _sanitize_strategy_or_setup(setup, _SETUP_MAX)
    s_symbol = _sanitize_symbol(symbol)

    if not s_strategy or not s_setup or not s_symbol:
        raise ValueError(
            f"client_order_id sanitization stripped a segment "
            f"(strategy={s_strategy!r} setup={s_setup!r} symbol={s_symbol!r})"
        )

    uuid8 = uuid.uuid4().hex[:_UUID_LEN]
    coid = SEPARATOR.join((PREFIX, s_strategy, s_setup, s_symbol, role, uuid8))
    if len(coid) > MAX_LENGTH:
        # Defensive — should be impossible given the per-segment caps.
        raise ValueError(f"client_order_id length {len(coid)} exceeds {MAX_LENGTH}")
    return coid


def parse_client_order_id(coid: str | None) -> dict | None:
    """Parse a COID into its components. Returns None on any malformed input."""
    if not coid or not isinstance(coid, str):
        return None
    parts = coid.split(SEPARATOR)
    if len(parts) != 6:
        return None
    prefix, strategy, setup, symbol, role, uuid8 = parts
    if prefix != PREFIX:
        return None
    if not strategy or not setup or not symbol:
        return None
    if role not in _VALID_ROLES:
        return None
    if not _RE_UUID.match(uuid8):
        return None
    return {
        "strategy": strategy,
        "setup": setup,
        "symbol": symbol,
        "role": role,
        "uuid": uuid8,
    }
