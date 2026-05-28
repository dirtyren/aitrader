# Broker↔MySQL Reconciliation v2 — Plan 2: `client_order_id` Contract

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Tag every order submitted via `OrderExecutor` with a structured `client_order_id` of the form `aitrader__<strategy>__<setup>__<symbol>__<role>__<uuid8>`, persist it on `OpenPosition` and the MySQL `positions`/`trades` rows. After this plan, every position MySQL knows about has unambiguous `(strategy, setup)` attribution from the broker fill, which is what makes Plan 3's per-strategy reconciler possible.

**Architecture:** Single chokepoint helper module `broker/client_order_id.py` makes/parses COIDs. `OrderExecutor` learns the strategy name once at construction time, wraps every `submit_order` / `submit_bracket_order` / close call with a freshly minted COID, and threads the result into `OpenPosition.client_order_id` and the `MySQLStore.position_opened`/`position_closed` write paths. `AlpacaClient`'s submit methods get a new `client_order_id` parameter that they forward to Alpaca's REST payload. `state/reconciler.py` (the interim per-strategy reconciler still running until Plan 3) starts tagging adopted orphans with synthetic `role=adopted` COIDs so the DB invariant "every newly-written `positions` row has a parseable COID" holds.

**Tech Stack:** Python 3.12, SQLAlchemy 2.x ORM, pytest, the existing Alpaca REST wrapper in `broker/alpaca_client.py`, the existing test patterns in `tests/test_order_executor.py` (MagicMock-based) and `tests/test_alpaca_client_orders.py`.

**Spec:** `docs/superpowers/specs/2026-05-28-broker-mysql-reconciliation-design.md` §4 (`client_order_id` contract, MySQL changes, single chokepoint, COID columns).

**Builds on:** Plan 1 (already merged) added the `client_order_id` and `exit_client_order_id` columns to `positions` and `trades`, plus the `legacy_untagged` flag for pre-migration rows. Plan 2 starts populating those columns.

---

## File Structure

**Create:**
- `broker/client_order_id.py` — single source of truth for the COID format. Pure functions, no I/O. Exports `make_client_order_id`, `parse_client_order_id`, `Role` enum or constants, and a `MAX_LENGTH = 128` constant. ~80 lines.
- `tests/test_client_order_id.py` — round-trip + sanitization + length-cap + parse-edge-case tests. ~120 lines.

**Modify:**
- `broker/alpaca_client.py:229-249` (`submit_order`) and `:251-274` (`submit_bracket_order`) — both gain a `client_order_id: str | None = None` parameter and include it in the JSON payload when provided.
- `broker/order_executor.py:28-35` (`OrderExecutor.__init__`) — gain `strategy_name: str` parameter, store on instance. Every submit path inside `submit()` and `close_position()` and `_move_equity_stop_to_breakeven()` mints and forwards a COID.
- `state/position_book.py:19-36` (`OpenPosition` dataclass) — add `client_order_id: str | None = None` field.
- `state/mysql_store.py:300-320` (`_pos_to_dict`) — include `client_order_id` from `pos.client_order_id`. `:323-339` (`_dict_to_pos`) — read it back. `:265-355` (`position_closed`) — accept `exit_client_order_id: str | None = None` parameter and write to both the `positions` and `trades` rows.
- `state/reconciler.py:330-372` (equity adoption) and `:411-447` (crypto adoption) — mint a synthetic `role=adopted` COID for each adopted `OpenPosition` so the MySQL row has a parseable `client_order_id`.
- `main.py:460` (`OrderExecutor(...)` instantiation) — pass `strategy_name=system_name`.

**Test updates:**
- `tests/test_order_executor.py` — every existing test now constructs `OrderExecutor(client, book, strategy_name="vwap_wave", logger=...)`. New assertions on the COID being passed through to the AlpacaClient mock.
- `tests/test_order_executor_actions.py` — same `strategy_name` plumbing for any constructor call.
- `tests/test_alpaca_client_orders.py` — new test asserting `client_order_id` lands in the POST payload.
- `tests/test_reconciler.py` — adopted-position tests assert a synthetic COID is set.

**Untouched (intentionally — these belong to Plan 3 / Plan 4):**
- The new `reconciler/` service module.
- `MySQLStore` query methods that consume COIDs (`apply_tagged_fill`, etc.).
- `scripts/reconcile_resolve.py`.
- Dashboard tab.

---

## Roles enum & format

Five role values, used by both makers and the parser:

| Role | When |
|---|---|
| `entry` | Opening order (equity bracket entry, crypto market entry). |
| `exit` | Engine-initiated market close (`close_position`, time-stop close). |
| `stop` | Standalone stop order. *(Reserved for future; Plan 2 doesn't emit one — equity stops live inside the bracket and reuse the parent's COID family.)* |
| `target` | Standalone target/take-profit order (crypto TP limit). |
| `adopted` | Synthetic, used only by the reconciler for orphan adoption. |

Format: `aitrader__<strategy>__<setup>__<symbol>__<role>__<uuid8>` (5 `__` separators, 6 segments; the literal `aitrader` prefix doubles as a parser sanity check).

Length budget: `aitrader` (8) + 5×`__` (10) + strategy (≤32) + setup (≤32) + symbol (≤16) + role (≤7) + uuid8 (8) = 113. Under Alpaca's 128-char COID cap with margin.

Sanitization:
- `strategy`, `setup`, `role`: lowercase, allowed `[a-z0-9_]`, anything else → `_`. Length-clipped to 32 / 32 / 7.
- `symbol`: broker-flat form (`BTCUSD`, not `BTC/USD`); uppercased; allowed `[A-Z0-9]`, anything else dropped. Length-clipped to 16.
- `uuid8`: `uuid.uuid4().hex[:8]`, hex lowercase, fixed length 8.

---

## Task 1: Create `broker/client_order_id.py` + tests

**Files:**
- Create: `broker/client_order_id.py`
- Create: `tests/test_client_order_id.py`

- [ ] **Step 1: Write the failing test file**

Create `tests/test_client_order_id.py`:

```python
"""Tests for the client_order_id (COID) format helpers.

The COID format is the canonical attribution mechanism for orders submitted
to Alpaca. Every order's COID encodes (strategy, setup, symbol, role) so
fills can be matched back to the originating MySQL row by Plan 3's reconciler.
"""
from __future__ import annotations

import re

import pytest

from broker.client_order_id import (
    MAX_LENGTH,
    Role,
    make_client_order_id,
    parse_client_order_id,
)


# ── Roles ──────────────────────────────────────────────────────────────


def test_role_values_are_canonical():
    assert Role.ENTRY == "entry"
    assert Role.EXIT == "exit"
    assert Role.STOP == "stop"
    assert Role.TARGET == "target"
    assert Role.ADOPTED == "adopted"


def test_make_rejects_unknown_role():
    with pytest.raises(ValueError, match="role"):
        make_client_order_id("vwap_wave", "vwap_bounce", "BTCUSD", "rebalance")


# ── make_client_order_id ───────────────────────────────────────────────


def test_make_produces_expected_shape():
    coid = make_client_order_id("vwap_wave", "vwap_bounce", "BTCUSD", Role.ENTRY)
    parts = coid.split("__")
    assert parts[0] == "aitrader"
    assert parts[1] == "vwap_wave"
    assert parts[2] == "vwap_bounce"
    assert parts[3] == "BTCUSD"
    assert parts[4] == "entry"
    assert re.fullmatch(r"[0-9a-f]{8}", parts[5])


def test_make_under_max_length():
    # Worst-case-ish inputs; result must still fit within Alpaca's 128 cap.
    coid = make_client_order_id(
        "a_very_long_strategy_name_with_lots_of_chars_xxxxxx",
        "an_equally_long_setup_name_zzzzzzzzzzzzzzzzzzzzzzz",
        "VERYLONGSYMBOL12",
        Role.ENTRY,
    )
    assert len(coid) <= MAX_LENGTH


def test_make_lowercases_strategy_setup_role():
    coid = make_client_order_id("VWAP_Wave", "VWAP_Bounce", "btcusd", Role.ENTRY)
    parts = coid.split("__")
    assert parts[1] == "vwap_wave"
    assert parts[2] == "vwap_bounce"
    # Symbol is forced uppercase
    assert parts[3] == "BTCUSD"


def test_make_replaces_disallowed_chars_with_underscore():
    coid = make_client_order_id("vwap-wave!", "v.bounce", "AAPL", Role.ENTRY)
    parts = coid.split("__")
    assert parts[1] == "vwap_wave_"
    assert parts[2] == "v_bounce"


def test_make_strips_slash_from_symbol():
    coid = make_client_order_id("vwap_wave", "vwap_bounce", "BTC/USD", Role.ENTRY)
    parts = coid.split("__")
    assert parts[3] == "BTCUSD"


def test_make_uniqueness_via_uuid_suffix():
    a = make_client_order_id("vwap_wave", "vwap_bounce", "AAPL", Role.ENTRY)
    b = make_client_order_id("vwap_wave", "vwap_bounce", "AAPL", Role.ENTRY)
    assert a != b
    assert a.split("__")[:5] == b.split("__")[:5]


def test_make_empty_inputs_rejected():
    with pytest.raises(ValueError):
        make_client_order_id("", "vwap_bounce", "AAPL", Role.ENTRY)
    with pytest.raises(ValueError):
        make_client_order_id("vwap_wave", "", "AAPL", Role.ENTRY)
    with pytest.raises(ValueError):
        make_client_order_id("vwap_wave", "vwap_bounce", "", Role.ENTRY)


# ── parse_client_order_id ─────────────────────────────────────────────


def test_parse_round_trip():
    coid = make_client_order_id("vwap_wave", "vwap_bounce", "BTCUSD", Role.EXIT)
    parsed = parse_client_order_id(coid)
    assert parsed == {
        "strategy": "vwap_wave",
        "setup": "vwap_bounce",
        "symbol": "BTCUSD",
        "role": "exit",
        "uuid": parsed["uuid"],
    }
    assert re.fullmatch(r"[0-9a-f]{8}", parsed["uuid"])


def test_parse_returns_none_for_non_aitrader_prefix():
    assert parse_client_order_id("foo__vwap_wave__bounce__AAPL__entry__abcd1234") is None
    assert parse_client_order_id("AITRADER__vwap_wave__bounce__AAPL__entry__abcd1234") is None
    assert parse_client_order_id("") is None
    assert parse_client_order_id(None) is None  # type: ignore[arg-type]


def test_parse_returns_none_for_bad_segment_count():
    # Missing role + uuid
    assert parse_client_order_id("aitrader__vwap_wave__bounce__AAPL") is None
    # Extra trailing segment
    assert parse_client_order_id("aitrader__vwap_wave__bounce__AAPL__entry__abcd1234__extra") is None


def test_parse_returns_none_for_unknown_role():
    bad = "aitrader__vwap_wave__bounce__AAPL__rebalance__abcd1234"
    assert parse_client_order_id(bad) is None


def test_parse_returns_none_for_bad_uuid():
    bad = "aitrader__vwap_wave__bounce__AAPL__entry__nothex12"
    assert parse_client_order_id(bad) is None


def test_parse_returns_none_for_empty_segment():
    bad = "aitrader__vwap_wave____AAPL__entry__abcd1234"  # empty setup
    assert parse_client_order_id(bad) is None
```

- [ ] **Step 2: Run the test file — confirm it fails for the right reason**

```bash
pytest tests/test_client_order_id.py -v
```

Expected: `ImportError` / `ModuleNotFoundError` on `broker.client_order_id` (we haven't written it yet). This proves the test file's imports are wired correctly.

- [ ] **Step 3: Write `broker/client_order_id.py`**

Create the implementation file:

```python
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
```

- [ ] **Step 4: Run the test file — expect all green**

```bash
pytest tests/test_client_order_id.py -v
```

Expected: 14/14 PASS.

If any test fails, read the failure carefully — most likely cause is a sanitizer regex mismatch or an off-by-one on the length cap. Fix and re-run.

- [ ] **Step 5: Run the broader local test suite to confirm no regressions**

```bash
pytest tests/test_client_order_id.py tests/test_mysql_legacy_migration.py tests/test_mysql_schema_migration.py tests/test_position_book.py tests/test_daily_ledger.py tests/test_circuit_breakers.py -v
```

Expected: ALL PASS (no other code paths import the new module yet — should be clean).

- [ ] **Step 6: Commit**

```bash
git add broker/client_order_id.py tests/test_client_order_id.py
git commit -m "feat(broker): client_order_id helpers — make/parse with sanitization"
```

---

## Task 2: Add `client_order_id` to `OpenPosition`

**Files:**
- Modify: `state/position_book.py:19-36` (`OpenPosition` dataclass)
- Modify: `tests/test_position_book.py` (add a single test asserting the new field defaults to `None`)

The in-memory book only needs the *entry* COID — the *exit* COID is generated at close-time and passed directly to `position_closed`, never stored on `OpenPosition`.

- [ ] **Step 1: Add a failing test in `tests/test_position_book.py`**

Append this test to `tests/test_position_book.py` (do not delete or reorder anything else):

```python
def test_open_position_client_order_id_defaults_to_none():
    from datetime import datetime, timezone
    from state.position_book import OpenPosition

    pos = OpenPosition(
        symbol="AAPL", setup="vwap_bounce", side="long", qty=1.0,
        entry_px=100.0, stop_px=99.0, target_px=101.0,
        opened_at=datetime(2026, 5, 28, tzinfo=timezone.utc), order_id="o1",
    )
    assert pos.client_order_id is None


def test_open_position_client_order_id_can_be_set():
    from datetime import datetime, timezone
    from state.position_book import OpenPosition

    pos = OpenPosition(
        symbol="AAPL", setup="vwap_bounce", side="long", qty=1.0,
        entry_px=100.0, stop_px=99.0, target_px=101.0,
        opened_at=datetime(2026, 5, 28, tzinfo=timezone.utc), order_id="o1",
        client_order_id="aitrader__vwap_wave__vwap_bounce__AAPL__entry__abcd1234",
    )
    assert pos.client_order_id == "aitrader__vwap_wave__vwap_bounce__AAPL__entry__abcd1234"
```

- [ ] **Step 2: Run — confirm both new tests fail**

```bash
pytest tests/test_position_book.py::test_open_position_client_order_id_defaults_to_none tests/test_position_book.py::test_open_position_client_order_id_can_be_set -v
```

Expected: FAIL on `TypeError: __init__() got an unexpected keyword argument 'client_order_id'` (test 2) and `AttributeError` (test 1).

- [ ] **Step 3: Add the field to `OpenPosition`**

Open `/Users/alessandro.ren/dev/aitrader/state/position_book.py`. Locate the `@dataclass` `OpenPosition` (around line 19). After the `adopted: bool = False` line and BEFORE the `@property` definitions, add:

```python
    client_order_id: str | None = None     # COID stamped at order submit (Plan 2)
```

The full updated dataclass section should look like (only the new line is added; do not change other fields):

```python
@dataclass
class OpenPosition:
    symbol: str
    setup: str
    side: str
    qty: float
    entry_px: float
    stop_px: float | None
    target_px: float | None
    opened_at: datetime
    order_id: str
    breakeven_moved: bool = False
    bars_held: int = 0
    stop_order_id: str | None = None
    target_order_id: str | None = None
    initial_stop_px: float | None = None
    adopted: bool = False
    client_order_id: str | None = None     # COID stamped at order submit (Plan 2)
```

- [ ] **Step 4: Run the new tests + full position_book suite**

```bash
pytest tests/test_position_book.py -v
```

Expected: all PASS, including the two new tests.

- [ ] **Step 5: Commit**

```bash
git add state/position_book.py tests/test_position_book.py
git commit -m "feat(state): OpenPosition.client_order_id field"
```

---

## Task 3: MySQL store — round-trip `client_order_id` + accept `exit_client_order_id` in `position_closed`

**Files:**
- Modify: `state/mysql_store.py:300-320` (`_pos_to_dict`)
- Modify: `state/mysql_store.py:322-339` (`_dict_to_pos`)
- Modify: `state/mysql_store.py:265-355` (`position_closed`)
- Modify: `tests/test_mysql_legacy_migration.py` — the existing `_StubStore` does not exercise `_pos_to_dict`. Add a fresh test file `tests/test_mysql_store_coid.py` for the round-trip behavior (kept separate to avoid bloating the legacy-migration test file).

- [ ] **Step 1: Create `tests/test_mysql_store_coid.py` with failing tests**

Create this file. It uses an in-memory SQLite engine to actually exercise `_pos_to_dict` / `_dict_to_pos` / `position_closed` against real ORM rows.

```python
"""Tests for COID round-trip in MySQLStore (Plan 2).

Uses an in-memory SQLite engine to write and read positions and trades
with client_order_id / exit_client_order_id values. Bypasses _build_url
by constructing the engine directly.
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
    TradeRow,
)
from state.position_book import OpenPosition


@pytest.fixture
def store():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    s = MySQLStore.__new__(MySQLStore)
    s._engine = engine
    s.strategy_name = "vwap_wave"
    s._log = logging.getLogger("test_coid")
    # Pre-create the strategy row so foreign keys resolve
    with Session(engine) as session:
        session.add(StrategyRow(name="vwap_wave"))
        session.commit()
        s._strategy_id = session.query(StrategyRow.id).filter_by(name="vwap_wave").one()[0]
    return s


def _pos(coid: str | None = "aitrader__vwap_wave__vwap_bounce__AAPL__entry__abcd1234"):
    return OpenPosition(
        symbol="AAPL", setup="vwap_bounce", side="long", qty=1.0,
        entry_px=100.0, stop_px=99.0, target_px=101.0,
        opened_at=datetime(2026, 5, 28, 14, 0, tzinfo=timezone.utc),
        order_id="o1",
        initial_stop_px=99.0,
        client_order_id=coid,
    )


def test_position_opened_persists_client_order_id(store):
    store.position_opened(_pos(), "equity")
    with Session(store._engine) as session:
        row = session.query(PositionRow).one()
        assert row.client_order_id == "aitrader__vwap_wave__vwap_bounce__AAPL__entry__abcd1234"


def test_load_open_positions_round_trips_client_order_id(store):
    store.position_opened(_pos(), "equity")
    book = store.load_open_positions()
    pos = book.get("AAPL", "vwap_bounce")
    assert pos is not None
    assert pos.client_order_id == "aitrader__vwap_wave__vwap_bounce__AAPL__entry__abcd1234"


def test_position_closed_writes_exit_client_order_id_to_positions_and_trades(store):
    store.position_opened(_pos(), "equity")
    exit_coid = "aitrader__vwap_wave__vwap_bounce__AAPL__exit__deadbeef"
    result = store.position_closed(
        symbol="AAPL",
        exit_px=102.0,
        close_reason="target",
        setup_name="vwap_bounce",
        exit_client_order_id=exit_coid,
    )
    assert result is not None
    with Session(store._engine) as session:
        pos_row = session.query(PositionRow).one()
        assert pos_row.status == "closed"
        assert pos_row.exit_client_order_id == exit_coid
        # Entry COID preserved on the closed positions row
        assert pos_row.client_order_id == "aitrader__vwap_wave__vwap_bounce__AAPL__entry__abcd1234"

        trade_row = session.query(TradeRow).one()
        assert trade_row.exit_client_order_id == exit_coid
        assert trade_row.client_order_id == "aitrader__vwap_wave__vwap_bounce__AAPL__entry__abcd1234"


def test_position_opened_without_coid_persists_null(store):
    """Backwards-compat: pre-Plan-2 callers (until rollout completes) skip COID."""
    store.position_opened(_pos(coid=None), "equity")
    with Session(store._engine) as session:
        row = session.query(PositionRow).one()
        assert row.client_order_id is None


def test_position_closed_without_exit_coid_persists_null(store):
    store.position_opened(_pos(), "equity")
    store.position_closed(
        symbol="AAPL", exit_px=102.0, close_reason="target",
        setup_name="vwap_bounce",
    )
    with Session(store._engine) as session:
        pos_row = session.query(PositionRow).one()
        assert pos_row.exit_client_order_id is None
        trade_row = session.query(TradeRow).one()
        assert trade_row.exit_client_order_id is None
```

- [ ] **Step 2: Run — confirm 5 tests fail**

```bash
pytest tests/test_mysql_store_coid.py -v
```

Expected: failures because `_pos_to_dict` doesn't write `client_order_id`, `_dict_to_pos` doesn't read it, and `position_closed` doesn't accept `exit_client_order_id`.

- [ ] **Step 3: Update `_pos_to_dict` to include `client_order_id`**

In `state/mysql_store.py`, locate `_pos_to_dict` (around line 300). Add `"client_order_id": pos.client_order_id,` to the returned dict — place it immediately after the `"order_id"` line:

```python
    @staticmethod
    def _pos_to_dict(pos: OpenPosition, asset_class: str,
                     strategy_id: int) -> dict:
        return {
            "strategy_id": strategy_id,
            "symbol": pos.symbol,
            "asset_class": asset_class,
            "side": pos.side,
            "qty": Decimal(str(pos.qty)),
            "entry_px": Decimal(str(pos.entry_px)),
            "stop_px": Decimal(str(pos.stop_px)) if pos.stop_px is not None else None,
            "target_px": Decimal(str(pos.target_px)) if pos.target_px is not None else None,
            "initial_stop_px": Decimal(str(pos.initial_stop_px)) if pos.initial_stop_px is not None else None,
            "setup_name": pos.setup,
            "order_id": pos.order_id or "",
            "client_order_id": pos.client_order_id,
            "stop_order_id": pos.stop_order_id or None,
            "breakeven_moved": pos.breakeven_moved,
            "bars_held": pos.bars_held,
            "adopted": pos.adopted,
            "status": "open",
            "opened_at": pos.opened_at,
        }
```

- [ ] **Step 4: Update `_dict_to_pos` to read `client_order_id`**

In `state/mysql_store.py`, locate `_dict_to_pos` (around line 322). Add `client_order_id=row.client_order_id,` to the constructor call — place it immediately after the `order_id` line:

```python
    @staticmethod
    def _dict_to_pos(row: PositionRow) -> OpenPosition:
        return OpenPosition(
            symbol=row.symbol,
            setup=row.setup_name,
            side=row.side,
            qty=float(row.qty),
            entry_px=float(row.entry_px),
            stop_px=float(row.stop_px) if row.stop_px is not None else None,
            target_px=float(row.target_px) if row.target_px is not None else None,
            opened_at=row.opened_at,
            order_id=row.order_id or "",
            client_order_id=row.client_order_id,
            stop_order_id=row.stop_order_id,
            initial_stop_px=float(row.initial_stop_px) if row.initial_stop_px is not None else None,
            breakeven_moved=row.breakeven_moved,
            bars_held=row.bars_held,
            adopted=row.adopted,
        )
```

- [ ] **Step 5: Update `position_closed` to accept and persist `exit_client_order_id`**

In `state/mysql_store.py`, locate `position_closed` (around line 265). Add the new parameter to the signature and write it to both `PositionRow` and `TradeRow`. The full updated method (you're modifying the signature line and adding two `exit_client_order_id=...` lines on the trade construction; the rest is unchanged):

Change the signature from:

```python
    def position_closed(
        self,
        symbol: str,
        exit_px: float,
        close_reason: str,
        closed_at: datetime | None = None,
        setup_name: str | None = None,
    ) -> dict | None:
```

to:

```python
    def position_closed(
        self,
        symbol: str,
        exit_px: float,
        close_reason: str,
        closed_at: datetime | None = None,
        setup_name: str | None = None,
        exit_client_order_id: str | None = None,
    ) -> dict | None:
```

Inside the method, after the line `row.bars_held = row.bars_held  # keep last known` and before the `# Archive to trades table` comment, add:

```python
            row.exit_client_order_id = exit_client_order_id
```

And in the `TradeRow(...)` constructor call inside the same method, add two lines just before `pnl_usd=pnl_usd,`:

```python
                client_order_id=row.client_order_id,
                exit_client_order_id=exit_client_order_id,
```

After the change, the `TradeRow(...)` block should read:

```python
            trade = TradeRow(
                strategy_id=self.strategy_id,
                symbol=row.symbol,
                asset_class=row.asset_class,
                setup_name=row.setup_name,
                side=row.side,
                qty=row.qty,
                entry_px=row.entry_px,
                exit_px=exit_dec,
                stop_px=row.stop_px,
                target_px=row.target_px,
                initial_stop_px=row.initial_stop_px,
                client_order_id=row.client_order_id,
                exit_client_order_id=exit_client_order_id,
                pnl_usd=pnl_usd,
                R_realized=R_realized,
                close_reason=close_reason,
                opened_at=row.opened_at,
                closed_at=closed_at,
                bars_held=row.bars_held,
            )
```

- [ ] **Step 6: Run the new tests — expect all green**

```bash
pytest tests/test_mysql_store_coid.py -v
```

Expected: 5/5 PASS.

- [ ] **Step 7: Run broader suite to confirm no regressions**

```bash
pytest tests/test_mysql_store_coid.py tests/test_mysql_schema_migration.py tests/test_mysql_legacy_migration.py tests/test_position_book.py tests/test_daily_ledger.py tests/test_circuit_breakers.py -v
```

Expected: all PASS.

- [ ] **Step 8: Commit**

```bash
git add state/mysql_store.py tests/test_mysql_store_coid.py
git commit -m "feat(state): MySQL round-trip for client_order_id + exit_client_order_id"
```

---

## Task 4: `AlpacaClient.submit_order` and `submit_bracket_order` accept `client_order_id`

**Files:**
- Modify: `broker/alpaca_client.py:229-249` (`submit_order`)
- Modify: `broker/alpaca_client.py:251-274` (`submit_bracket_order`)
- Modify: `tests/test_alpaca_client_orders.py` (add tests asserting COID is forwarded in payload)

Note: `replace_order` already has a `client_order_id` parameter (we saw it at line 289 of `alpaca_client.py`) — it stays as-is, no change needed.

- [ ] **Step 1: Read the existing test pattern**

Open `tests/test_alpaca_client_orders.py` and identify the pattern: typically tests use `requests_mock` or a fake `_request` to capture the payload sent to `/v2/orders`. Match that pattern when writing the new tests.

- [ ] **Step 2: Add failing tests to `tests/test_alpaca_client_orders.py`**

Append these two tests at the end of `tests/test_alpaca_client_orders.py`. They follow whatever pattern the existing tests use — adapt the fixture/setup helpers but keep the assertions exactly as below:

```python
def test_submit_order_forwards_client_order_id(monkeypatch):
    """COID supplied to submit_order must appear in the POST payload."""
    from broker.alpaca_client import AlpacaClient

    captured: dict = {}

    class _StubResponse:
        def json(self):
            return {"id": "ord-1"}

    def _fake_request(self, method, path, params=None, json=None):
        captured["method"] = method
        captured["path"] = path
        captured["json"] = json
        return _StubResponse()

    monkeypatch.setattr(AlpacaClient, "_request", _fake_request)
    client = AlpacaClient.__new__(AlpacaClient)  # bypass __init__ (no env)

    client.submit_order(
        symbol="AAPL", qty=1, side="buy",
        order_type="market", time_in_force="day",
        client_order_id="aitrader__vwap_wave__vwap_bounce__AAPL__entry__abcd1234",
    )
    assert captured["json"]["client_order_id"] == \
        "aitrader__vwap_wave__vwap_bounce__AAPL__entry__abcd1234"


def test_submit_order_omits_client_order_id_when_not_provided(monkeypatch):
    from broker.alpaca_client import AlpacaClient

    captured: dict = {}

    class _StubResponse:
        def json(self):
            return {"id": "ord-1"}

    def _fake_request(self, method, path, params=None, json=None):
        captured["json"] = json
        return _StubResponse()

    monkeypatch.setattr(AlpacaClient, "_request", _fake_request)
    client = AlpacaClient.__new__(AlpacaClient)

    client.submit_order(
        symbol="AAPL", qty=1, side="buy",
        order_type="market", time_in_force="day",
    )
    assert "client_order_id" not in captured["json"]


def test_submit_bracket_order_forwards_client_order_id(monkeypatch):
    from broker.alpaca_client import AlpacaClient

    captured: dict = {}

    class _StubResponse:
        def json(self):
            return {"id": "ord-1"}

    def _fake_request(self, method, path, params=None, json=None):
        captured["json"] = json
        return _StubResponse()

    monkeypatch.setattr(AlpacaClient, "_request", _fake_request)
    client = AlpacaClient.__new__(AlpacaClient)

    client.submit_bracket_order(
        symbol="AAPL", qty=10, side="buy",
        limit_price=100.0, stop_loss=99.0, take_profit=101.0,
        client_order_id="aitrader__vwap_wave__vwap_bounce__AAPL__entry__abcd1234",
    )
    assert captured["json"]["client_order_id"] == \
        "aitrader__vwap_wave__vwap_bounce__AAPL__entry__abcd1234"
```

- [ ] **Step 3: Run — confirm all three tests fail**

```bash
pytest tests/test_alpaca_client_orders.py -k "client_order_id" -v
```

Expected: FAIL with `TypeError: submit_order() got an unexpected keyword argument 'client_order_id'` (and similar for bracket).

- [ ] **Step 4: Add `client_order_id` to `submit_order`**

In `broker/alpaca_client.py`, modify `submit_order` (around line 229). Replace the entire method with this version (only the signature gains a parameter and the payload gets one new conditional line):

```python
    def submit_order(
        self,
        symbol: str,
        qty: float,
        side: str,
        order_type: str = "market",
        time_in_force: str = "day",
        limit_price: float | None = None,
        client_order_id: str | None = None,
    ) -> dict:
        """POST /v2/orders — submit a new order and return the order dict."""
        payload = {
            "symbol": symbol,
            "qty": qty,
            "side": side,
            "type": order_type,
            "time_in_force": time_in_force,
        }
        if limit_price is not None:
            payload["limit_price"] = _round_to_tick(limit_price)
        if client_order_id is not None:
            payload["client_order_id"] = client_order_id
        response = self._request("POST", "/v2/orders", json=payload)
        return response.json()
```

- [ ] **Step 5: Add `client_order_id` to `submit_bracket_order`**

In `broker/alpaca_client.py`, modify `submit_bracket_order` (around line 251). Replace with this version:

```python
    def submit_bracket_order(
        self,
        symbol: str,
        qty: float,
        side: str,
        limit_price: float,
        stop_loss: float,
        take_profit: float,
        time_in_force: str = "day",
        client_order_id: str | None = None,
    ) -> dict:
        """POST /v2/orders with order_class='bracket' (entry as limit + OCO stop/target)."""
        payload = {
            "symbol": symbol,
            "qty": qty,
            "side": side,
            "type": "limit",
            "limit_price": _round_to_tick(limit_price),
            "time_in_force": time_in_force,
            "order_class": "bracket",
            "stop_loss": {"stop_price": _round_to_tick(stop_loss)},
            "take_profit": {"limit_price": _round_to_tick(take_profit)},
        }
        if client_order_id is not None:
            payload["client_order_id"] = client_order_id
        response = self._request("POST", "/v2/orders", json=payload)
        return response.json()
```

- [ ] **Step 6: Run — expect green**

```bash
pytest tests/test_alpaca_client_orders.py -v
```

Expected: all tests pass (existing + the 3 new ones).

- [ ] **Step 7: Commit**

```bash
git add broker/alpaca_client.py tests/test_alpaca_client_orders.py
git commit -m "feat(broker): AlpacaClient submit/bracket forward client_order_id"
```

---

## Task 5: `OrderExecutor` — accept `strategy_name`, tag every submit with a COID

**Files:**
- Modify: `broker/order_executor.py:28-35` (constructor)
- Modify: `broker/order_executor.py:58-171` (`submit`)
- Modify: `broker/order_executor.py:173-199` (`close_position`)
- Modify: `broker/order_executor.py:201-261` (`handle_actions` — passes through `close_position`, no direct change)
- Modify: `main.py:460` (instantiation)
- Modify: `tests/test_order_executor.py` (constructor signature in every test)
- Modify: `tests/test_order_executor_actions.py` (constructor signature)

This is the heart of the plan. Every order out the door gets a COID. The `OpenPosition` returned from `submit()` carries the entry COID. `close_position()` and time-stop closes generate a fresh `role=exit` COID and return it to the caller (so the eventual `position_closed` MySQL call can include it — Plan 3 will hook this up; Plan 2 just makes the wire ready).

- [ ] **Step 1: Update existing `OrderExecutor` tests for the new constructor signature**

The existing tests at `tests/test_order_executor.py` and `tests/test_order_executor_actions.py` construct `OrderExecutor(client, book, logger=...)`. We're adding a required `strategy_name` parameter. Update each construction site to pass `strategy_name="vwap_wave"`.

In `tests/test_order_executor.py`, find every line of the form `ex = OrderExecutor(client, book, ...)` and change it to include `strategy_name="vwap_wave"`. There are 9 such call sites listed earlier — update each. Example pattern, applied to all:

```python
ex = OrderExecutor(client, book, strategy_name="vwap_wave", logger=MagicMock())
```

In `tests/test_order_executor_actions.py`, find:

```python
ex = OrderExecutor(client, book, logger=MagicMock())
```

and update to:

```python
ex = OrderExecutor(client, book, strategy_name="vwap_wave", logger=MagicMock())
```

- [ ] **Step 2: Add new failing tests asserting COIDs are emitted**

Append to `tests/test_order_executor.py` (do not delete or reorder existing tests):

```python
def test_submit_equity_passes_coid_to_bracket_order():
    """Equity submit must mint a role=entry COID and pass it to submit_bracket_order."""
    client = MagicMock()
    client.submit_bracket_order.return_value = {"id": "ord-1"}
    book = PositionBook()
    ex = OrderExecutor(client, book, strategy_name="vwap_wave", logger=MagicMock())
    decision = RiskDecision(approved=True, qty=10, notional=1000)

    pos = ex.submit(_signal(), decision, asset_class="equity")

    assert pos is not None
    assert client.submit_bracket_order.called
    coid = client.submit_bracket_order.call_args.kwargs["client_order_id"]
    assert coid is not None and coid.startswith("aitrader__vwap_wave__price_discovery__AAPL__entry__")
    # Position carries the same COID
    assert pos.client_order_id == coid


def test_submit_crypto_passes_coid_to_market_order_and_tp_limit():
    """Crypto submit mints role=entry on market entry and role=target on TP limit."""
    client = MagicMock()
    client.submit_order.return_value = {"id": "ord-2"}
    book = PositionBook()
    ex = OrderExecutor(client, book, strategy_name="vwap_wave", logger=MagicMock())
    decision = RiskDecision(approved=True, qty=0.1, notional=5000)

    pos = ex.submit(_signal(symbol="BTC/USD", side="long"), decision, asset_class="crypto")

    assert pos is not None
    assert client.submit_order.call_count == 2
    entry_call = client.submit_order.call_args_list[0]
    tp_call = client.submit_order.call_args_list[1]

    entry_coid = entry_call.kwargs["client_order_id"]
    tp_coid = tp_call.kwargs["client_order_id"]
    assert entry_coid.startswith("aitrader__vwap_wave__price_discovery__BTCUSD__entry__")
    assert tp_coid.startswith("aitrader__vwap_wave__price_discovery__BTCUSD__target__")
    # Position carries the entry COID, not the TP one
    assert pos.client_order_id == entry_coid


def test_close_position_passes_role_exit_coid():
    client = MagicMock()
    client.submit_order.return_value = {"id": "close-1"}
    book = PositionBook()
    ex = OrderExecutor(client, book, strategy_name="vwap_wave", logger=MagicMock())

    result = ex.close_position(symbol="BTCUSD", side="long", qty=0.5)

    assert result == {"id": "close-1"}
    client.submit_order.assert_called_once()
    coid = client.submit_order.call_args.kwargs["client_order_id"]
    assert coid.startswith("aitrader__vwap_wave___unknown__BTCUSD__exit__")
```

The third test deliberately uses `_unknown` for the setup field — `close_position` doesn't know which setup the position belongs to (its current signature is symbol/side/qty only). We document this gap intentionally; Plan 3 will narrow it when the reconciler service supersedes this exit path. For the COID to remain parseable, we still need a non-empty setup segment, so `_unknown` is the constant we use. (The leading underscore is allowed in `[a-z0-9_]`.)

- [ ] **Step 3: Run — confirm new tests fail**

```bash
pytest tests/test_order_executor.py -v
```

Expected: existing tests fail because the constructor changed (until you update the test file in Step 1 — do that first if you haven't), and the three new tests fail because the executor doesn't yet emit COIDs.

If you ran Step 1 already, only the three new tests should fail.

- [ ] **Step 4: Update `OrderExecutor.__init__` to accept `strategy_name`**

In `broker/order_executor.py`, replace the `__init__` method (around line 28) with:

```python
    def __init__(self, alpaca_client, book: PositionBook,
                 strategy_name: str,
                 logger: logging.Logger | None = None,
                 mysql_store=None):
        if not strategy_name:
            raise ValueError("OrderExecutor requires a non-empty strategy_name")
        self.client = alpaca_client
        self.book = book
        self.strategy_name = strategy_name
        self.logger = logger or logging.getLogger("vwap_wave.executor")
        self._dtbp_exhausted = False
        self._mysql = mysql_store
```

- [ ] **Step 5: Add the COID import at the top of `broker/order_executor.py`**

Just below the existing `from notifications import send_position_open_alert` line, add:

```python
from broker.client_order_id import Role, make_client_order_id
```

- [ ] **Step 6: Tag the equity bracket entry**

In `broker/order_executor.py`, inside `submit()`, locate the equity branch (around line 83 — `if asset_class == "equity":`). Before the `submit_bracket_order` call, mint a COID and pass it through. Replace the equity branch with:

```python
            if asset_class == "equity":
                entry_coid = make_client_order_id(
                    self.strategy_name, signal.setup, signal.symbol, Role.ENTRY,
                )
                order = self.client.submit_bracket_order(
                    symbol=signal.symbol,
                    qty=decision.qty,
                    side=alp_side,
                    limit_price=signal.entry,
                    stop_loss=signal.stop,
                    take_profit=signal.target,
                    time_in_force="day",
                    client_order_id=entry_coid,
                )
```

- [ ] **Step 7: Tag the crypto market entry**

In the same `submit()` method, replace the crypto branch with:

```python
            elif asset_class == "crypto":
                entry_coid = make_client_order_id(
                    self.strategy_name, signal.setup, signal.symbol, Role.ENTRY,
                )
                # Crypto: market entry + engine-managed virtual stop/target
                order = self.client.submit_order(
                    symbol=signal.symbol,
                    qty=decision.qty,
                    side=alp_side,
                    order_type="market",
                    time_in_force="gtc",
                    client_order_id=entry_coid,
                )
```

After this change the variable `entry_coid` is in scope for both equity and crypto branches when we reach the position construction.

- [ ] **Step 8: Tag the crypto TP limit order**

Just below the entry submission, the existing code submits a separate limit TP for crypto. Update that block with a `role=target` COID:

```python
        # For crypto, place limit TP order immediately
        if asset_class == "crypto" and signal.target is not None:
            try:
                tp_side = "sell" if alp_side == "buy" else "buy"
                tp_coid = make_client_order_id(
                    self.strategy_name, signal.setup, signal.symbol, Role.TARGET,
                )
                tp_order = self.client.submit_order(
                    symbol=signal.symbol,
                    qty=decision.qty,
                    side=tp_side,
                    order_type="limit",
                    limit_price=round(signal.target, 4),
                    time_in_force="gtc",
                    client_order_id=tp_coid,
                )
                target_order_id = tp_order.get("id")
            except Exception as exc:
                self.logger.error("Failed to place crypto TP limit order for %s: %s", signal.symbol, exc)
```

- [ ] **Step 9: Stamp the entry COID on the constructed `OpenPosition`**

A few lines further down, the `OpenPosition(...)` constructor receives the new field:

```python
        pos = OpenPosition(
            symbol=signal.symbol, setup=signal.setup, side=signal.side,
            qty=decision.qty, entry_px=signal.entry, stop_px=signal.stop,
            target_px=signal.target, opened_at=signal.ts,
            order_id=order.get("id", ""),
            stop_order_id=stop_order_id,
            target_order_id=target_order_id,
            initial_stop_px=signal.stop,
            client_order_id=entry_coid,
        )
```

- [ ] **Step 10: Tag `close_position`'s market close**

In `broker/order_executor.py`, replace `close_position` (around line 173) with:

```python
    def close_position(self, symbol: str, side: str, qty: float) -> dict | None:
        """Submit a market close order. Used for virtual stops / time stops.

        The COID uses setup='_unknown' because this path doesn't know which
        setup owned the position. Plan 3's reconciler service supersedes this
        exit path and will use the real setup.
        """
        exit_coid = make_client_order_id(
            self.strategy_name, "_unknown", symbol, Role.EXIT,
        )
        try:
            return self.client.submit_order(
                symbol=symbol, qty=qty,
                side="sell" if side == "long" else "buy",
                order_type="market", time_in_force="gtc",
                client_order_id=exit_coid,
            )
        except Exception as exc:
            if "insufficient qty" in str(exc).lower() or "not enough" in str(exc).lower() or "qty" in str(exc).lower():
                self.logger.warning("CLOSE_QTY_MISMATCH symbol=%s qty=%s, attempting full position close", symbol, qty)
                try:
                    positions = self.client.get_positions()
                    broker_pos = next((p for p in positions if p["symbol"].replace("/", "") == symbol.replace("/", "")), None)
                    if broker_pos:
                        actual_qty = abs(float(broker_pos["qty"]))
                        if actual_qty > 0:
                            return self.client.submit_order(
                                symbol=symbol, qty=actual_qty,
                                side="sell" if side == "long" else "buy",
                                order_type="market", time_in_force="gtc",
                                client_order_id=exit_coid,
                            )
                except Exception as inner_exc:
                    self.logger.error("CLOSE_FULL_POSITION_FAILED symbol=%s error=%s", symbol, inner_exc)

            self.logger.error("CLOSE_FAILED symbol=%s error=%s", symbol, exc, exc_info=True)
            return None
```

- [ ] **Step 11: Update `main.py` instantiation**

In `/Users/alessandro.ren/dev/aitrader/main.py`, locate the line at ~460:

```python
    executor = OrderExecutor(alpaca, book, logger=logger, mysql_store=mysql)
```

Replace with:

```python
    executor = OrderExecutor(alpaca, book, strategy_name=system_name,
                             logger=logger, mysql_store=mysql)
```

- [ ] **Step 12: Run the OrderExecutor tests**

```bash
pytest tests/test_order_executor.py tests/test_order_executor_actions.py -v
```

Expected: all PASS, including the three new COID tests.

If failures: most common cause is the close_position test — confirm the exit COID prefix matches `aitrader__vwap_wave___unknown__BTCUSD__exit__` (note the **double underscore** between strategy and `_unknown` — that's the segment separator, plus the underscore in `_unknown`).

- [ ] **Step 13: Run broader local suite**

```bash
pytest tests/test_client_order_id.py tests/test_order_executor.py tests/test_order_executor_actions.py tests/test_alpaca_client_orders.py tests/test_position_book.py tests/test_mysql_store_coid.py tests/test_mysql_schema_migration.py tests/test_mysql_legacy_migration.py tests/test_daily_ledger.py tests/test_circuit_breakers.py -v
```

Expected: all PASS.

- [ ] **Step 14: Commit**

```bash
git add broker/order_executor.py main.py tests/test_order_executor.py tests/test_order_executor_actions.py
git commit -m "feat(broker): OrderExecutor mints client_order_id on every submit"
```

---

## Task 6: Reconciler — tag adopted positions with `role=adopted` COIDs

**Files:**
- Modify: `state/reconciler.py:330-372` (equity adoption — the `OpenPosition(...)` constructor block in `for symbol in orphan_equity_symbols:`)
- Modify: `state/reconciler.py:411-447` (crypto adoption — the `OpenPosition(...)` constructor block in `for broker_pos in orphan_crypto_records:`)
- Modify: `tests/test_reconciler.py` — add an assertion that adopted positions have a parseable adopted-role COID

This is interim. Plan 3 deletes `state/reconciler.py` outright. But during the rollout window, this reconciler keeps running, and any position it adopts and writes to MySQL must have a non-NULL `client_order_id` so it doesn't get caught by the `legacy_untagged` backfill or, worse, leave a NULL COID on a row written *after* Plan 1 deployed (which would violate the new invariant "all newly-written positions have COIDs").

- [ ] **Step 1: Add a failing test in `tests/test_reconciler.py`**

Append the following test (do not delete or reorder anything else):

```python
def test_adopted_equity_position_has_role_adopted_coid():
    """Adoption must stamp a parseable role=adopted COID on the OpenPosition."""
    from unittest.mock import MagicMock
    from state.reconciler import Reconciler
    from state.position_book import PositionBook
    from broker.client_order_id import parse_client_order_id

    alpaca = MagicMock()
    alpaca.get_positions.return_value = [{
        "symbol": "AAPL",
        "qty": "10",
        "side": "long",
        "avg_entry_price": "100.00",
        "asset_class": "us_equity",
    }]
    alpaca.list_orders.return_value = []  # no bracket data

    mysql = MagicMock()
    mysql.strategy_name = "vwap_wave"
    mysql.close_positions_not_in_broker.return_value = []
    mysql.count_strategies_holding.return_value = 0

    book = PositionBook()
    rec = Reconciler(alpaca, mysql_store=mysql, configured_symbols=["AAPL"])
    rec.reconcile(book)

    pos = book.get("AAPL")
    assert pos is not None
    parsed = parse_client_order_id(pos.client_order_id)
    assert parsed is not None, f"adopted position COID is not parseable: {pos.client_order_id!r}"
    assert parsed["strategy"] == "vwap_wave"
    assert parsed["setup"] == "adopted"
    assert parsed["symbol"] == "AAPL"
    assert parsed["role"] == "adopted"


def test_adopted_crypto_position_has_role_adopted_coid():
    from unittest.mock import MagicMock
    from state.reconciler import Reconciler
    from state.position_book import PositionBook
    from broker.client_order_id import parse_client_order_id

    alpaca = MagicMock()
    alpaca.get_positions.return_value = [{
        "symbol": "BTCUSD",
        "qty": "0.5",
        "side": "long",
        "avg_entry_price": "50000.00",
        "current_price": "50100.00",
        "asset_class": "crypto",
    }]
    alpaca.get_crypto_bars.return_value = []  # no bars; ATR computation skipped

    mysql = MagicMock()
    mysql.strategy_name = "vwap_wave"
    mysql.close_positions_not_in_broker.return_value = []
    mysql.count_strategies_holding.return_value = 0

    book = PositionBook()
    rec = Reconciler(alpaca, mysql_store=mysql, configured_symbols=["BTCUSD"])
    rec.reconcile(book)

    pos = book.get("BTCUSD")
    assert pos is not None
    parsed = parse_client_order_id(pos.client_order_id)
    assert parsed is not None
    assert parsed["role"] == "adopted"
    assert parsed["symbol"] == "BTCUSD"
```

- [ ] **Step 2: Run — confirm both tests fail**

```bash
pytest tests/test_reconciler.py::test_adopted_equity_position_has_role_adopted_coid tests/test_reconciler.py::test_adopted_crypto_position_has_role_adopted_coid -v
```

Expected: FAIL because adopted positions currently have `client_order_id = None`.

- [ ] **Step 3: Add the COID import to `state/reconciler.py`**

At the top of the import block (find the existing imports — they include `from state.position_book import OpenPosition, PositionBook`), add:

```python
from broker.client_order_id import Role, make_client_order_id
```

- [ ] **Step 4: Tag adopted equity positions**

In `state/reconciler.py`, inside the equity-adoption block (`for symbol in orphan_equity_symbols:`), locate the `pos = OpenPosition(...)` constructor (around line 330). Add the `client_order_id=` argument:

```python
                pos = OpenPosition(
                    symbol=symbol,
                    setup="adopted",
                    side=_normalize_side(broker_pos["side"]),
                    qty=abs(float(broker_pos["qty"])),
                    entry_px=float(broker_pos["avg_entry_price"]),
                    stop_px=stop_px,
                    target_px=target_px,
                    opened_at=datetime.now(timezone.utc),
                    order_id="",
                    stop_order_id=stop_order_id,
                    initial_stop_px=stop_px,
                    adopted=True,
                    client_order_id=make_client_order_id(
                        self._mysql.strategy_name if self._mysql else "unknown",
                        "adopted", symbol, Role.ADOPTED,
                    ),
                )
```

- [ ] **Step 5: Tag adopted crypto positions**

Same change for the crypto-adoption block (`for broker_pos in orphan_crypto_records:`, around line 411):

```python
                pos = OpenPosition(
                    symbol=symbol,
                    setup="adopted",
                    side=side,
                    qty=qty,
                    entry_px=entry_px,
                    stop_px=stop_px,
                    target_px=target_px,
                    opened_at=datetime.now(timezone.utc),
                    order_id="",
                    stop_order_id=None,
                    initial_stop_px=stop_px,
                    adopted=True,
                    client_order_id=make_client_order_id(
                        self._mysql.strategy_name if self._mysql else "unknown",
                        "adopted", symbol, Role.ADOPTED,
                    ),
                )
```

- [ ] **Step 6: Run — confirm tests pass**

```bash
pytest tests/test_reconciler.py -v -k "adopted"
```

Expected: both new tests PASS, plus any existing adopted-related tests still pass.

- [ ] **Step 7: Run the full reconciler suite**

```bash
pytest tests/test_reconciler.py -v
```

Expected: all PASS. Some pre-existing adoption tests may have weak assertions on the constructor — they should keep passing because we only *added* a field.

- [ ] **Step 8: Commit**

```bash
git add state/reconciler.py tests/test_reconciler.py
git commit -m "feat(reconciler): tag adopted positions with role=adopted COID"
```

---

## Task 7: End-to-end smoke + final test sweep

**Files:** none modified (verification only).

- [ ] **Step 1: Run the entire affected test surface**

```bash
pytest \
  tests/test_client_order_id.py \
  tests/test_position_book.py \
  tests/test_mysql_store_coid.py \
  tests/test_mysql_schema_migration.py \
  tests/test_mysql_legacy_migration.py \
  tests/test_alpaca_client_orders.py \
  tests/test_order_executor.py \
  tests/test_order_executor_actions.py \
  tests/test_reconciler.py \
  tests/test_daily_ledger.py \
  tests/test_circuit_breakers.py \
  -v
```

Expected: all PASS. Number of tests: existing + the new ones added across Tasks 1, 2, 3, 4, 5, 6.

If `tests/test_reconciler.py` or `tests/test_order_executor.py` has any pre-existing test that constructs the object with positional args (no kwargs), the constructor change in Task 5 step 4 would break it. Inspect any failure carefully and add `strategy_name="..."` if needed.

- [ ] **Step 2: Run the full pytest suite (skip pre-existing requests/pytz import errors)**

```bash
pytest --ignore=tests/test_alpaca_client_bars.py --ignore=tests/test_alpaca_client_list_orders.py --ignore=tests/test_alpaca_data.py --ignore=tests/test_intraday_replay.py 2>&1 | tail -5
```

(Skip files that fail at import on the local machine because of missing `requests`/`pytz` — these run fine inside docker.)

Expected: green summary, e.g. `60 passed`.

- [ ] **Step 3: Manual end-to-end COID round-trip in the running stack**

This verifies the wire is fully connected — a real submitted order ends up with a parseable COID in the MySQL row. Optional but high-value before opening the PR; takes ~3 minutes.

```bash
# Bring up a fresh stack
docker compose down -v
docker compose up -d mysql
# wait for healthy
until docker compose exec -T mysql mysqladmin ping --silent; do sleep 2; done

# Build trader image with the new code
docker compose build trader
```

Then run a one-shot Python script inside the trader image that exercises the chokepoint:

```bash
docker compose run --rm trader python -c "
from datetime import datetime, timezone
from unittest.mock import MagicMock
from state.mysql_store import MySQLStore
from state.position_book import OpenPosition, PositionBook
from broker.order_executor import OrderExecutor
from broker.client_order_id import parse_client_order_id
from strategies.base_setup import SetupSignal
from risk.manager import RiskDecision

mysql = MySQLStore('vwap_wave')
mysql.ensure_schema()
mysql.upsert_strategy()

fake_alpaca = MagicMock()
fake_alpaca.submit_bracket_order.return_value = {'id': 'ord-test', 'legs': []}

book = PositionBook()
ex = OrderExecutor(fake_alpaca, book, strategy_name='vwap_wave', mysql_store=mysql)

sig = SetupSignal(
    setup='vwap_bounce', symbol='AAPL', side='long',
    entry=100.0, stop=99.0, target=101.0, atr=1.0, level=100.0,
    ts=datetime.now(timezone.utc),
)
decision = RiskDecision(approved=True, qty=1, notional=100)
pos = ex.submit(sig, decision, asset_class='equity')
assert pos is not None, 'submit returned None'
print(f'in-memory COID: {pos.client_order_id}')
assert parse_client_order_id(pos.client_order_id) is not None

# Now read it back from MySQL
fresh_book = mysql.load_open_positions()
loaded = fresh_book.get('AAPL', 'vwap_bounce')
assert loaded is not None, 'position not found after reload'
print(f'persisted COID: {loaded.client_order_id}')
parsed = parse_client_order_id(loaded.client_order_id)
assert parsed is not None
assert parsed['strategy'] == 'vwap_wave'
assert parsed['setup'] == 'vwap_bounce'
assert parsed['symbol'] == 'AAPL'
assert parsed['role'] == 'entry'
print('OK')
" 2>&1 | tail -20
```

Expected output ends with `OK` — and the printed COIDs should be identical and parseable.

- [ ] **Step 4: Tear down**

```bash
docker compose down -v
```

- [ ] **Step 5: No commit needed**

This task is verification-only.

---

## Self-review checklist

**Spec coverage:**
- §4 contract format `aitrader__strategy__setup__symbol__role__uuid8` → Task 1 (helper).
- §4 chokepoint enforcement (every order tagged) → Task 5 (executor) + Task 6 (reconciler interim adoption).
- §4 `client_order_id` MySQL columns populated on `position_opened` and `position_closed` → Task 3.
- §4 `OpenPosition.client_order_id` field → Task 2.
- §4 phase-1 deploy: every new order has a tagged COID → Task 5.
- §4 phase-2 in-flight legacy: existing rows already flagged `legacy_untagged=TRUE` by Plan 1's backfill — no new code needed in this plan; orphan adoption now tags COIDs (Task 6).
- §4 phase-3 broker-only post-deploy: deferred to Plan 3.
- §4 recovery from "submitted, filled, crashed before write": deferred to Plan 3 (the reconciler service does the auto-insert; this plan provides the COID tagging that makes that recovery possible).

**Type consistency:** `make_client_order_id` signature `(strategy, setup, symbol, role)` is used identically in Tasks 1, 5, 6. `Role.ENTRY` / `Role.EXIT` / `Role.TARGET` / `Role.ADOPTED` constants are introduced in Task 1 and used by their string value in Task 5 (via the `Role` namespace) and Task 6. `OpenPosition.client_order_id` is the field name everywhere. `position_closed`'s new param is `exit_client_order_id` everywhere.

**Placeholder scan:** No TBDs, no "implement later", no "similar to Task N" — every step has runnable code or commands. The only intentional placeholder string is `"_unknown"` for the close-path setup segment, which is explicitly documented as a known gap closed by Plan 3.

**Edge case coverage:**
- COID length cap: tested in Task 1.
- Sanitization (slashes in crypto symbols, mixed case, bad chars): tested in Task 1.
- COID NULL acceptance on legacy callers (until Plan 3 fully drains): tested in Task 3.
- Idempotency: Plan 1 already covered (`legacy_untagged=0` guard).

---

## Done When

- All 6 implementation tasks committed (Task 7 is verification-only).
- `pytest` green across the affected files.
- Manual smoke (Task 7 step 3) prints `OK`.
- Branch is mergeable to `main`.
- After merge: deploy to one trader container in staging for one trading session, observe `positions.client_order_id` populating on every new INSERT, no `MYSQL_MIGRATION_UNEXPECTED` warnings in logs.
- Plan 3 (reconciler service) becomes implementable.
