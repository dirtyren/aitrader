# Engine Exit Idempotency + Real-Setup Close COID Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop the engine from re-firing exit actions on positions whose close has already been submitted, and make every close COID carry the real setup name so the reconciler can match the close fill back to the open MySQL row.

**Architecture:** Symmetric to PR #92's `fill_confirmed` gate. Add an `exit_submitted` flag on `OpenPosition` and a matching column on `PositionRow`; flip it when `OrderExecutor.handle_actions` submits a broker close (or when an equity bracket OCO leg fires server-side); skip flagged positions in `PositionManager.on_bar`. Make `OrderExecutor.close_position` require a real `setup` so the exit COID parses back to the right `(strategy, setup, symbol)` triple at `reconciler/fills.py:apply_tagged_fill`. Patch the parallel idempotency hole in `_move_equity_stop_to_breakeven`. Ship a read-only audit script that detects cross-account drift between MySQL and broker.

**Tech Stack:** Python 3.12, SQLAlchemy 2.0 (MySQL via PyMySQL), pytest, dataclasses. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-06-02-engine-exit-idempotency-design.md`

---

## File Structure

### New files
- `scripts/audit_phantom_close_stacks.py` — read-only CLI: aggregates MySQL open rows by `(symbol, asset_class)`, fetches broker positions via `AlpacaRouter`, prints drift report.
- `tests/test_position_manager_exit_gate.py` — exit-gate behavior in `PositionManager.on_bar`.
- `tests/test_order_executor_exit_idempotency.py` — flag flip on time_stop / BRACKET_EXIT / crypto exits, cancel-failure path, close-failure path.
- `tests/test_close_coid_setup.py` — end-to-end: real-setup COID parses correctly and `reconciler/fills.py:apply_tagged_fill` matches the open row.
- `tests/test_audit_phantom_close_stacks.py` — fixture: MySQL=1 short, broker=22 long; report content asserted.

### Modified files
- `state/position_book.py` — add `exit_submitted: bool = False` to `OpenPosition`.
- `state/mysql_store.py` — add `PositionRow.exit_submitted` column, idempotent ALTER migration, `mark_exit_submitted` helper, `_pos_to_dict`/`_row_to_pos` round-trip.
- `core/position_manager.py` — exit gate in `on_bar` after the fill gate.
- `broker/order_executor.py` — `close_position(..., setup: str)` required; `handle_actions` flips flag on every exit branch; `_mark_exit_submitted` private helper; `_move_equity_stop_to_breakeven` sets `breakeven_moved=True` on success and on benign-fragment rejection.
- `reconciler/fills.py` — emit `reconciler_close_fill_unmatched` event when an exit-role fill has no matching open row.
- `state/strategy_close_all.py` — pass `setup` through to `submit_close_with_drift_recovery` (already builds COID locally; just sanity-audit for setup correctness).
- `state/operator_close.py` — same audit; this path uses `"cleanslate"` as setup intentionally for orphan flatten — leave unchanged but verify reconciler treats it via the new unmatched-event log.
- `scheduler/gap_and_go_loop.py:318` — `force_close_all` passes `pos.setup` to `close_position`.
- `tests/test_record_exits_to_ledger.py`, `tests/test_order_executor_actions.py`, `tests/test_safe_close.py`, `tests/test_post_open_attach.py`, `tests/test_gap_and_go_loop.py` — update for new `setup` arg on `close_position`.

---

## Task 1: `OpenPosition.exit_submitted` field + book round-trip

**Files:**
- Modify: `state/position_book.py:19-43`
- Test: `tests/test_position_book.py` (existing)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_position_book.py`:

```python
def test_open_position_exit_submitted_default_false():
    pos = OpenPosition(
        symbol="COIN", setup="price_discovery", side="short",
        qty=1.0, entry_px=174.07, stop_px=175.31, target_px=171.60,
        opened_at=datetime(2026, 6, 2, 15, 0, tzinfo=timezone.utc),
        order_id="abc",
    )
    assert pos.exit_submitted is False


def test_open_position_exit_submitted_settable():
    pos = OpenPosition(
        symbol="COIN", setup="price_discovery", side="short",
        qty=1.0, entry_px=174.07, stop_px=175.31, target_px=171.60,
        opened_at=datetime(2026, 6, 2, 15, 0, tzinfo=timezone.utc),
        order_id="abc",
        exit_submitted=True,
    )
    assert pos.exit_submitted is True
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/test_position_book.py::test_open_position_exit_submitted_default_false tests/test_position_book.py::test_open_position_exit_submitted_settable -v
```

Expected: FAIL with `TypeError: ... got an unexpected keyword argument 'exit_submitted'` (second test) and `AttributeError` (first test).

- [ ] **Step 3: Add the field**

In `state/position_book.py`, after the `fill_confirmed: bool = False` line (around line 43):

```python
    # True once OrderExecutor has submitted (or registered an in-flight
    # bracket OCO firing for) a broker close for this position. Gates
    # PositionManager.on_bar so the engine cannot re-emit time_stop /
    # virtual exits on a position whose close is already in flight.
    # Cleared only by the position leaving the book — i.e. the reconciler
    # closing the MySQL row from the broker's actual close fill.
    exit_submitted: bool = False
```

- [ ] **Step 4: Run test to verify it passes**

```bash
pytest tests/test_position_book.py -v
```

Expected: all pass, including the two new tests.

- [ ] **Step 5: Commit**

```bash
git add state/position_book.py tests/test_position_book.py
git commit -m "feat(book): add OpenPosition.exit_submitted gate flag"
```

---

## Task 2: `PositionRow.exit_submitted` column + migration + round-trip + `mark_exit_submitted`

**Files:**
- Modify: `state/mysql_store.py:74-99` (column), `state/mysql_store.py:285-310` (migration), `state/mysql_store.py:560-595` (round-trip), `state/mysql_store.py:596-616` (helper)
- Test: `tests/test_mysql_store_coid.py` (existing) — the integration test file that exercises the schema

- [ ] **Step 1: Write the failing test**

Append to `tests/test_mysql_store_coid.py` (or wherever fixture `mysql_store` lives — search for `mark_fill_confirmed` in tests to find the right file):

```python
def test_mark_exit_submitted_flips_flag(mysql_store, sample_open_position):
    # sample_open_position fixture inserts an open row with exit_submitted=False
    pos = sample_open_position
    ok = mysql_store.mark_exit_submitted(
        strategy_id=mysql_store.strategy_id,
        symbol=pos.symbol,
        setup_name=pos.setup,
    )
    assert ok is True
    book = mysql_store.load_open_positions()
    reloaded = book.get(pos.symbol, pos.setup)
    assert reloaded is not None
    assert reloaded.exit_submitted is True


def test_mark_exit_submitted_idempotent(mysql_store, sample_open_position):
    pos = sample_open_position
    mysql_store.mark_exit_submitted(
        strategy_id=mysql_store.strategy_id,
        symbol=pos.symbol, setup_name=pos.setup,
    )
    ok = mysql_store.mark_exit_submitted(
        strategy_id=mysql_store.strategy_id,
        symbol=pos.symbol, setup_name=pos.setup,
    )
    assert ok is True


def test_mark_exit_submitted_no_open_row_returns_false(mysql_store):
    ok = mysql_store.mark_exit_submitted(
        strategy_id=mysql_store.strategy_id,
        symbol="ZZZZ", setup_name="nonexistent",
    )
    assert ok is False


def test_load_open_positions_round_trips_exit_submitted(mysql_store, sample_open_position):
    pos = sample_open_position
    mysql_store.mark_exit_submitted(
        strategy_id=mysql_store.strategy_id,
        symbol=pos.symbol, setup_name=pos.setup,
    )
    book = mysql_store.load_open_positions()
    reloaded = book.get(pos.symbol, pos.setup)
    assert reloaded.exit_submitted is True
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/test_mysql_store_coid.py -v -k "exit_submitted"
```

Expected: FAIL with `AttributeError: 'MySQLStore' object has no attribute 'mark_exit_submitted'`.

- [ ] **Step 3: Add the column to `PositionRow`**

In `state/mysql_store.py`, after the `fill_confirmed` mapped column (around line 99):

```python
    # See OpenPosition.exit_submitted — once True, PositionManager.on_bar
    # treats this position as exit-in-flight and stops emitting further
    # virtual exit actions. The reconciler clears the row entirely when
    # the broker's close fill arrives.
    exit_submitted: Mapped[bool] = mapped_column(Boolean, default=False)
```

- [ ] **Step 4: Add the idempotent ALTER in `_run_migrations`**

Find the `fill_confirmed` ALTER block (around line 293) and add directly after it:

```python
            # positions.exit_submitted: gates engine virtual exits AFTER a
            # close has been submitted (the symmetric counterpart to
            # fill_confirmed gating BEFORE the entry is confirmed). Default
            # 0 for existing rows — they get one re-evaluation cycle, which
            # is fine because the close COID is now setup-tagged so the
            # reconciler will match the resulting close fill.
            session.execute(text(
                "ALTER TABLE positions ADD COLUMN exit_submitted TINYINT(1) "
                "NOT NULL DEFAULT 0"
            ))
            session.commit()
```

Wrap in the same `try/except` that the existing `fill_confirmed` ALTER uses (the surrounding pattern catches `OperationalError` for "Duplicate column name" so the migration is idempotent). Read the `fill_confirmed` block first and mirror its exact try/except shape.

- [ ] **Step 5: Round-trip the field**

In `_pos_to_dict` (around line 570), add:

```python
            "exit_submitted": pos.exit_submitted,
```

In `_row_to_pos` (around line 593), add:

```python
            exit_submitted=row.exit_submitted,
```

- [ ] **Step 6: Add `mark_exit_submitted`**

After the existing `mark_fill_confirmed` method (around line 616):

```python
    def mark_exit_submitted(
        self, strategy_id: int, symbol: str, setup_name: str,
    ) -> bool:
        """Flip an open position's exit_submitted flag to True.

        Called by OrderExecutor.handle_actions immediately after submitting
        (or registering an in-flight bracket OCO firing for) a broker close,
        so PositionManager stops emitting further exit actions on the next
        bar. Returns True if the row was found and updated.

        Idempotent: re-applying on an already-True row is a no-op success.
        """
        with Session(self._engine) as session:
            row = session.query(PositionRow).filter(
                PositionRow.strategy_id == strategy_id,
                PositionRow.symbol.in_(self._get_symbol_candidates(symbol)),
                PositionRow.setup_name == setup_name,
                PositionRow.status == "open",
            ).one_or_none()
            if row is None:
                return False
            row.exit_submitted = True
            session.commit()
            return True
```

- [ ] **Step 7: Run test to verify it passes**

```bash
pytest tests/test_mysql_store_coid.py -v
```

Expected: all four new tests pass; existing tests still green.

- [ ] **Step 8: Commit**

```bash
git add state/mysql_store.py tests/test_mysql_store_coid.py
git commit -m "feat(mysql_store): persist exit_submitted flag + mark_exit_submitted helper"
```

---

## Task 3: `PositionManager.on_bar` exit gate

**Files:**
- Modify: `core/position_manager.py:56-77` (the `on_bar` method)
- Test: Create `tests/test_position_manager_exit_gate.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_position_manager_exit_gate.py`:

```python
"""Exit-side fill gate — symmetric counterpart to test_position_manager_fill_gate.

Once OrderExecutor has flipped exit_submitted on a position, PositionManager
must stop emitting actions for that position on subsequent bars, AND must
not bump bars_held (so a stale time_stop can't re-fire if the flag flips
back somehow).
"""
from __future__ import annotations
from datetime import datetime, timezone

from core.bar import Bar
from core.position_manager import PositionManager
from state.position_book import OpenPosition, PositionBook


def _make_pos(**overrides) -> OpenPosition:
    base = dict(
        symbol="COIN", setup="price_discovery", side="short",
        qty=1.0, entry_px=174.07, stop_px=175.31, target_px=171.60,
        opened_at=datetime(2026, 6, 2, 15, 0, tzinfo=timezone.utc),
        order_id="abc",
        fill_confirmed=True,  # past the fill gate
        bars_held=0,
    )
    base.update(overrides)
    return OpenPosition(**base)


def _make_bar(close: float = 175.50) -> Bar:
    return Bar(
        ts=datetime(2026, 6, 2, 16, 5, tzinfo=timezone.utc),
        open=close, high=close, low=close, close=close, volume=1000,
    )


def test_on_bar_skips_position_with_exit_submitted_true():
    book = PositionBook()
    pos = _make_pos(exit_submitted=True)
    book.add(pos)
    pm = PositionManager(book=book, max_hold_bars=12, breakeven_at_R=1.0)

    actions = pm.on_bar("COIN", _make_bar(close=175.50))  # would normally hit stop

    assert actions == []


def test_on_bar_does_not_bump_bars_held_when_exit_submitted():
    book = PositionBook()
    pos = _make_pos(exit_submitted=True, bars_held=3)
    book.add(pos)
    pm = PositionManager(book=book, max_hold_bars=12, breakeven_at_R=1.0)

    pm.on_bar("COIN", _make_bar())

    assert book.get("COIN", "price_discovery").bars_held == 3


def test_on_bar_emits_when_exit_submitted_false():
    book = PositionBook()
    pos = _make_pos(exit_submitted=False)
    book.add(pos)
    pm = PositionManager(book=book, max_hold_bars=12, breakeven_at_R=1.0)

    actions = pm.on_bar("COIN", _make_bar(close=175.50))  # short, high≥stop

    assert len(actions) == 1
    assert actions[0].kind == "stop"


def test_on_bar_exit_gate_runs_after_fill_gate():
    """A position that's both unconfirmed-fill AND exit-submitted should
    skip at the fill gate and never reach the exit gate (well-defined
    behavior — gates compose; either gate skipping the position is fine)."""
    book = PositionBook()
    pos = _make_pos(fill_confirmed=False, exit_submitted=True)
    book.add(pos)
    pm = PositionManager(
        book=book, max_hold_bars=12, breakeven_at_R=1.0,
        order_status_for=lambda p: None,  # fill gate stays closed
    )

    actions = pm.on_bar("COIN", _make_bar(close=175.50))

    assert actions == []
    # bars_held also shouldn't change (fill gate's responsibility, but verify)
    assert book.get("COIN", "price_discovery").bars_held == 0
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/test_position_manager_exit_gate.py -v
```

Expected: FAIL — first test gets a `stop` action because the gate doesn't exist yet.

- [ ] **Step 3: Add the gate to `on_bar`**

In `core/position_manager.py`, find the existing `on_bar` method. After the `_confirm_fill` gate's `continue`, add:

```python
            if pos.exit_submitted:
                # Engine has already submitted (or registered as in-flight)
                # a broker close for this position. Defer everything until
                # the reconciler closes the MySQL row from the broker fill
                # and the next cycle's book reload drops it. bars_held is
                # NOT incremented — same shape as the fill gate.
                continue
```

The full ordered structure inside the `for pos in self._book.get_all(symbol):` loop becomes:
1. `if not self._confirm_fill(pos): continue`
2. `if pos.exit_submitted: continue`  ← NEW
3. `actions = self._check_position(pos, bar)`
4. `all_actions.extend(actions)`

- [ ] **Step 4: Run test to verify it passes**

```bash
pytest tests/test_position_manager_exit_gate.py -v
```

Expected: all four tests pass.

- [ ] **Step 5: Run the broader PM test suite to verify no regressions**

```bash
pytest tests/test_position_manager.py tests/test_position_manager_fill_gate.py tests/test_position_manager_exit_gate.py -v
```

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add core/position_manager.py tests/test_position_manager_exit_gate.py
git commit -m "feat(position_manager): exit_submitted gate skips post-close re-fires"
```

---

## Task 4: `OrderExecutor.close_position` requires real `setup`

**Files:**
- Modify: `broker/order_executor.py:281-312` (`close_position` signature + COID build)
- Test: Create `tests/test_close_coid_setup.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_close_coid_setup.py`:

```python
"""Close COID must carry the real setup name so reconciler/fills.py
apply_tagged_fill can match it back to the open MySQL row.

Pre-fix bug: close_position hardcoded setup="_unknown", so the close fill's
COID parsed to setup="unknown", find_open_position_by_setup returned None,
and the open row stayed open forever — driving the COIN incident on
2026-06-02.
"""
from __future__ import annotations
from unittest.mock import MagicMock

from broker.client_order_id import parse_client_order_id
from broker.order_executor import OrderExecutor
from state.position_book import PositionBook


def test_close_position_coid_carries_real_setup():
    client = MagicMock()
    client.submit_order.return_value = {"id": "ord-xyz"}
    book = PositionBook()
    ex = OrderExecutor(client, book, strategy_name="vwap_wave_equity",
                       logger=MagicMock(), mysql_store=None)

    ex.close_position(
        symbol="COIN", side="short", qty=1.0,
        setup="price_discovery", asset_class="equity",
    )

    submitted = client.submit_order.call_args
    coid = submitted.kwargs["client_order_id"]
    parsed = parse_client_order_id(coid)
    assert parsed is not None
    assert parsed["strategy"] == "vwap_wave_equity"
    assert parsed["setup"] == "price_discovery"
    assert parsed["symbol"] == "COIN"
    assert parsed["role"] == "X"  # Role.EXIT


def test_close_position_setup_is_required_keyword():
    client = MagicMock()
    book = PositionBook()
    ex = OrderExecutor(client, book, strategy_name="vwap_wave_equity",
                       logger=MagicMock(), mysql_store=None)

    import pytest
    with pytest.raises(TypeError):
        ex.close_position(symbol="COIN", side="short", qty=1.0,
                          asset_class="equity")
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/test_close_coid_setup.py -v
```

Expected: FAIL — first test gets `setup="unknown"`; second test passes (no error) because current signature accepts no setup.

- [ ] **Step 3: Update `close_position` signature**

In `broker/order_executor.py`, replace the current `close_position` (lines 281-312) with:

```python
    def close_position(
        self, symbol: str, side: str, qty: float,
        *,
        setup: str,
        asset_class: str = "crypto",
    ) -> dict | None:
        """Submit a market close order. Used for virtual / time stops.

        ``setup`` is required so the exit COID parses back to the
        (strategy, setup, symbol) triple at reconciler/fills.py
        :apply_tagged_fill — without it, the reconciler can't match the
        close fill to the open row and the row stays open indefinitely.
        See incident 2026-06-02 (COIN: 22 stacked broker positions vs 1
        open MySQL row) and design doc 2026-06-02-engine-exit-idempotency.

        ``asset_class`` controls the fee-drift safety margin: crypto closes
        shave ~1e-6 off the requested qty (fees drain from the asset side
        between snapshot and submit), equity passes through unchanged.
        """
        exit_coid = make_client_order_id(
            self.strategy_name, setup, symbol, Role.EXIT,
        )
        return submit_close_with_drift_recovery(
            client=self.client,
            symbol=symbol,
            qty=qty,
            side="sell" if side == "long" else "buy",
            client_order_id=exit_coid,
            asset_class=asset_class,
        )
```

The `*` makes `setup` keyword-only, which is required for the second test and also forces every call site to be explicit.

- [ ] **Step 4: Update `handle_actions` call sites in the same file**

In `broker/order_executor.py`, find the two `self.close_position(...)` calls inside `handle_actions` (around lines 349 and 368). Update both:

Equity time_stop branch (line ~349):

```python
                    self.close_position(a.symbol, a.side, a.qty,
                                        setup=a.setup,
                                        asset_class="equity")
```

Crypto branch (line ~368):

```python
                    self.close_position(a.symbol, a.side, a.qty,
                                        setup=a.setup,
                                        asset_class="crypto")
```

- [ ] **Step 5: Update existing test files for new keyword-only `setup`**

Search for `close_position(` callers in tests:

```bash
grep -rn "close_position(" tests/ --include="*.py"
```

For each match in `tests/test_order_executor.py`, `tests/test_order_executor_actions.py`, `tests/test_safe_close.py`, `tests/test_post_open_attach.py`, `tests/test_gap_and_go_loop.py`, add the `setup="<reasonable_value>"` keyword (use whatever setup the fixture's position has). If a test uses a generic mock without a position fixture, use `setup="test_setup"`.

- [ ] **Step 6: Update non-test call sites**

In `scheduler/gap_and_go_loop.py:318`:

```python
                self.executor.close_position(pos.symbol, pos.side, pos.qty,
                                             setup=pos.setup,
                                             asset_class="equity")
```

(`state/strategy_close_all.py` and `state/operator_close.py` do NOT use `OrderExecutor.close_position` — they call `submit_close_with_drift_recovery` directly with a COID they build locally. Verify by re-running `grep -rn "executor.close_position\|\.close_position(" --include="*.py"` after the change; only `gap_and_go_loop.py` and the executor's own internals should match.)

- [ ] **Step 7: Run tests to verify all pass**

```bash
pytest tests/test_close_coid_setup.py tests/test_order_executor.py tests/test_order_executor_actions.py tests/test_safe_close.py tests/test_post_open_attach.py tests/test_gap_and_go_loop.py -v
```

Expected: all pass.

- [ ] **Step 8: Commit**

```bash
git add broker/order_executor.py scheduler/gap_and_go_loop.py tests/
git commit -m "fix(order_executor): require real setup on close_position so reconciler matches close fill"
```

---

## Task 5: `OrderExecutor` flips `exit_submitted` on every exit branch

**Files:**
- Modify: `broker/order_executor.py:314-376` (`handle_actions`), add private `_mark_exit_submitted` helper
- Test: Create `tests/test_order_executor_exit_idempotency.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_order_executor_exit_idempotency.py`:

```python
"""OrderExecutor.handle_actions flips exit_submitted exactly once per
logical exit, both in-memory (book) and persisted (mysql_store).

This is the runtime side of the engine-side fix for the 2026-06-02 COIN
incident. The PositionManager exit gate (test_position_manager_exit_gate)
relies on this flag being flipped by handle_actions; if it isn't, the
gate never closes and the loop returns.
"""
from __future__ import annotations
from datetime import datetime, timezone
from unittest.mock import MagicMock

from broker.order_executor import OrderExecutor
from core.position_manager import PositionAction
from state.position_book import OpenPosition, PositionBook


def _seed(book: PositionBook, *, exit_submitted: bool = False) -> OpenPosition:
    pos = OpenPosition(
        symbol="COIN", setup="price_discovery", side="short",
        qty=1.0, entry_px=174.07, stop_px=175.31, target_px=171.60,
        opened_at=datetime(2026, 6, 2, 15, 0, tzinfo=timezone.utc),
        order_id="parent-1",
        fill_confirmed=True,
        exit_submitted=exit_submitted,
    )
    book.add(pos)
    return pos


def _make_executor(client, book, mysql_store=None) -> OrderExecutor:
    return OrderExecutor(
        client, book,
        strategy_name="vwap_wave_equity",
        logger=MagicMock(),
        mysql_store=mysql_store,
    )


def test_equity_time_stop_flips_flag_in_book_and_mysql():
    client = MagicMock()
    client.submit_order.return_value = {"id": "close-1"}
    book = PositionBook()
    pos = _seed(book)
    mysql = MagicMock()
    mysql.strategy_id = 42
    ex = _make_executor(client, book, mysql_store=mysql)

    action = PositionAction(
        symbol="COIN", setup="price_discovery", side="short",
        qty=1.0, kind="time_stop", price=174.27,
    )
    ex.handle_actions([action], asset_class="equity",
                      parent_order_id="parent-1")

    assert book.get("COIN", "price_discovery").exit_submitted is True
    mysql.mark_exit_submitted.assert_called_once_with(
        strategy_id=42, symbol="COIN", setup_name="price_discovery",
    )


def test_equity_bracket_exit_flips_flag_without_submitting_close():
    """For equity stop/target, the broker bracket OCO fires server-side.
    We only need to flip the flag — no close to submit."""
    client = MagicMock()
    book = PositionBook()
    _seed(book)
    mysql = MagicMock()
    mysql.strategy_id = 42
    ex = _make_executor(client, book, mysql_store=mysql)

    action = PositionAction(
        symbol="COIN", setup="price_discovery", side="short",
        qty=1.0, kind="stop", price=175.31,
    )
    ex.handle_actions([action], asset_class="equity",
                      parent_order_id="parent-1")

    client.submit_order.assert_not_called()  # OCO is broker-side
    assert book.get("COIN", "price_discovery").exit_submitted is True
    mysql.mark_exit_submitted.assert_called_once()


def test_equity_time_stop_cancel_failure_still_flips_and_submits_close():
    """Today's COIN bug: cancel raised because parent was already canceled,
    and the engine kept re-firing because exit_submitted didn't flip.
    With the fix, cancel-fail is logged-and-proceeded."""
    client = MagicMock()
    client.cancel_order.side_effect = Exception(
        "order already in canceled status"
    )
    client.submit_order.return_value = {"id": "close-1"}
    book = PositionBook()
    _seed(book)
    mysql = MagicMock()
    mysql.strategy_id = 42
    ex = _make_executor(client, book, mysql_store=mysql)

    action = PositionAction(
        symbol="COIN", setup="price_discovery", side="short",
        qty=1.0, kind="time_stop", price=174.27,
    )
    ex.handle_actions([action], asset_class="equity",
                      parent_order_id="parent-1")

    assert client.submit_order.called  # close still submitted
    assert book.get("COIN", "price_discovery").exit_submitted is True


def test_close_submission_failure_does_not_flip_flag():
    """If close_position itself raises, leave exit_submitted=False so the
    next cycle retries. The PR doesn't expand failure-mode coverage —
    pre-existing behavior preserved."""
    client = MagicMock()
    client.submit_order.side_effect = Exception("alpaca 500")
    book = PositionBook()
    _seed(book)
    mysql = MagicMock()
    mysql.strategy_id = 42
    ex = _make_executor(client, book, mysql_store=mysql)

    action = PositionAction(
        symbol="COIN", setup="price_discovery", side="short",
        qty=1.0, kind="time_stop", price=174.27,
    )
    # The current code path doesn't catch close failures — it bubbles.
    # Assert we don't pre-mark exit_submitted before the close completes.
    try:
        ex.handle_actions([action], asset_class="equity",
                          parent_order_id="parent-1")
    except Exception:
        pass
    assert book.get("COIN", "price_discovery").exit_submitted is False
    mysql.mark_exit_submitted.assert_not_called()


def test_crypto_time_stop_flips_flag():
    client = MagicMock()
    client.submit_order.return_value = {"id": "close-1"}
    book = PositionBook()
    pos = OpenPosition(
        symbol="BTC/USD", setup="vwap_bands", side="long",
        qty=0.05, entry_px=70000.0, stop_px=69000.0, target_px=71500.0,
        opened_at=datetime(2026, 6, 2, 15, 0, tzinfo=timezone.utc),
        order_id="parent-2", fill_confirmed=True,
    )
    book.add(pos)
    mysql = MagicMock()
    mysql.strategy_id = 99
    ex = OrderExecutor(client, book, strategy_name="vwap_bands_crypto",
                       logger=MagicMock(), mysql_store=mysql)

    action = PositionAction(
        symbol="BTC/USD", setup="vwap_bands", side="long",
        qty=0.05, kind="time_stop", price=69500.0,
    )
    ex.handle_actions([action], asset_class="crypto", parent_order_id=None)

    assert book.get("BTC/USD", "vwap_bands").exit_submitted is True
    mysql.mark_exit_submitted.assert_called_once_with(
        strategy_id=99, symbol="BTC/USD", setup_name="vwap_bands",
    )
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/test_order_executor_exit_idempotency.py -v
```

Expected: FAIL — `book.get(...).exit_submitted is True` is False; `mysql.mark_exit_submitted` not called.

- [ ] **Step 3: Add `_mark_exit_submitted` private helper**

In `broker/order_executor.py`, before `_move_equity_stop_to_breakeven` (around line 377):

```python
    def _mark_exit_submitted(self, symbol: str, setup: str) -> None:
        """Flip exit_submitted=True on the in-memory book and persist to
        MySQL so PositionManager.on_bar stops emitting further exits for
        this position. Idempotent — safe to call repeatedly.
        """
        pos = self.book.get(symbol, setup)
        if pos is not None:
            pos.exit_submitted = True
        if self.mysql_store is not None:
            try:
                self.mysql_store.mark_exit_submitted(
                    strategy_id=self.mysql_store.strategy_id,
                    symbol=symbol, setup_name=setup,
                )
            except Exception as exc:
                self.logger.error(
                    "MARK_EXIT_SUBMITTED_FAILED symbol=%s setup=%s error=%s",
                    symbol, setup, exc, exc_info=True,
                )
```

- [ ] **Step 4: Update `handle_actions` to call the helper**

In `broker/order_executor.py`, modify the body of `handle_actions` (lines 328-375):

**Equity stop/target branch** — currently logs `BRACKET_EXIT` only. Add the flag flip:

```python
            if asset_class == "equity":
                if a.kind in ("stop", "target"):
                    self._mark_exit_submitted(a.symbol, a.setup)
                    self.logger.info(
                        "BRACKET_EXIT symbol=%s kind=%s price=%.4f setup=%s",
                        a.symbol, a.kind, a.price, a.setup,
                    )
                    continue
```

**Equity time_stop branch** — change cancel-failure from `error` to `warning`, do not re-raise (already doesn't), then submit close, then flip flag:

```python
                if a.kind == "time_stop":
                    if parent_order_id:
                        try:
                            self.client.cancel_order(parent_order_id)
                        except Exception as exc:
                            self.logger.warning(
                                "CANCEL_FAILED_DURING_TIME_STOP symbol=%s "
                                "order_id=%s error=%s — treating parent as "
                                "already terminal, proceeding with close",
                                a.symbol, parent_order_id, exc,
                            )
                    self.close_position(a.symbol, a.side, a.qty,
                                        setup=a.setup,
                                        asset_class="equity")
                    self._mark_exit_submitted(a.symbol, a.setup)
                    self.logger.info(
                        "TIME_STOP symbol=%s side=%s qty=%s setup=%s",
                        a.symbol, a.side, a.qty, a.setup,
                    )
                    continue
```

**Crypto stop/target/time_stop branch** — flip flag after close:

```python
            elif asset_class == "crypto":
                if a.kind in ("stop", "target", "time_stop"):
                    pos = self.book.get(a.symbol, a.setup)
                    if pos and getattr(pos, "target_order_id", None):
                        try:
                            self.client.cancel_order(pos.target_order_id)
                        except Exception as exc:
                            self.logger.error(
                                "CANCEL_TP_FAILED symbol=%s order_id=%s error=%s",
                                a.symbol, pos.target_order_id, exc,
                            )
                    self.close_position(a.symbol, a.side, a.qty,
                                        setup=a.setup,
                                        asset_class="crypto")
                    self._mark_exit_submitted(a.symbol, a.setup)
                    self.logger.info(
                        "VIRTUAL_EXIT symbol=%s kind=%s price=%.4f qty=%s setup=%s",
                        a.symbol, a.kind, a.price, a.qty, a.setup,
                    )
                    continue
```

- [ ] **Step 5: Run test to verify it passes**

```bash
pytest tests/test_order_executor_exit_idempotency.py -v
```

Expected: all five tests pass.

- [ ] **Step 6: Run wider executor test suite**

```bash
pytest tests/test_order_executor.py tests/test_order_executor_actions.py -v
```

Expected: all pass. If `test_order_executor_actions.py` asserts on the `BRACKET_EXIT` or `TIME_STOP` log message format and the new `setup=` suffix breaks it, update the assertion to match.

- [ ] **Step 7: Commit**

```bash
git add broker/order_executor.py tests/test_order_executor_exit_idempotency.py tests/test_order_executor_actions.py
git commit -m "fix(order_executor): flip exit_submitted on every close path so engine stops re-firing"
```

---

## Task 6: `_move_equity_stop_to_breakeven` idempotency

**Files:**
- Modify: `broker/order_executor.py:377-397`
- Test: `tests/test_order_executor.py` (existing — find the breakeven test or add one)

- [ ] **Step 1: Write the failing test**

Add to `tests/test_order_executor.py` (or wherever `_move_equity_stop_to_breakeven` is tested — search for `BREAKEVEN` in tests):

```python
def test_breakeven_replace_success_sets_breakeven_moved():
    client = MagicMock()
    client.replace_order.return_value = {"id": "stop-leg-1"}
    book = PositionBook()
    pos = OpenPosition(
        symbol="COIN", setup="price_discovery", side="short",
        qty=1.0, entry_px=174.07, stop_px=175.31, target_px=171.60,
        opened_at=datetime(2026, 6, 2, 15, 0, tzinfo=timezone.utc),
        order_id="parent-1", stop_order_id="stop-leg-1",
        fill_confirmed=True, breakeven_moved=False,
    )
    book.add(pos)
    ex = OrderExecutor(client, book, strategy_name="vwap_wave_equity",
                       logger=MagicMock(), mysql_store=None)

    action = PositionAction(
        symbol="COIN", setup="price_discovery", side="short",
        qty=1.0, kind="breakeven", price=174.07,
    )
    ex.handle_actions([action], asset_class="equity")

    assert book.get("COIN", "price_discovery").breakeven_moved is True


def test_breakeven_replace_already_replaced_sets_breakeven_moved():
    """Today's COIN log: BREAKEVEN_REPLACE_FAILED ... order already replaced
    — looped because breakeven_moved didn't flip. The benign-fragment
    branch must mark the move as done so the engine stops retrying."""
    from broker.order_executor import OrderRejectedError
    client = MagicMock()
    client.replace_order.side_effect = OrderRejectedError(
        "order already replaced"
    )
    book = PositionBook()
    pos = OpenPosition(
        symbol="COIN", setup="price_discovery", side="short",
        qty=1.0, entry_px=174.07, stop_px=175.31, target_px=171.60,
        opened_at=datetime(2026, 6, 2, 15, 0, tzinfo=timezone.utc),
        order_id="parent-1", stop_order_id="stop-leg-1",
        fill_confirmed=True, breakeven_moved=False,
    )
    book.add(pos)
    ex = OrderExecutor(client, book, strategy_name="vwap_wave_equity",
                       logger=MagicMock(), mysql_store=None)

    action = PositionAction(
        symbol="COIN", setup="price_discovery", side="short",
        qty=1.0, kind="breakeven", price=174.07,
    )
    ex.handle_actions([action], asset_class="equity")

    assert book.get("COIN", "price_discovery").breakeven_moved is True
```

If `OrderRejectedError` and `_BENIGN_BREAKEVEN_FRAGMENTS` aren't easily importable, adapt the test to use whatever fixture pattern existing tests use. Check existing breakeven tests first.

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/test_order_executor.py -v -k "breakeven"
```

Expected: FAIL — `breakeven_moved` stays False.

- [ ] **Step 3: Update `_move_equity_stop_to_breakeven`**

In `broker/order_executor.py`, modify the method (around lines 377-397):

```python
    def _move_equity_stop_to_breakeven(self, a: PositionAction) -> None:
        pos = self.book.get(a.symbol, a.setup)
        stop_leg = pos.stop_order_id if pos else None
        if not stop_leg:
            self.logger.warning("BREAKEVEN_NO_STOP_LEG symbol=%s — skipping replace", a.symbol)
            return
        try:
            self.client.replace_order(stop_leg, stop_price=a.price)
            if pos is not None:
                pos.breakeven_moved = True
            self.logger.info("BREAKEVEN_REPLACED symbol=%s stop_leg=%s new_stop=%.4f",
                             a.symbol, stop_leg, a.price)
        except OrderRejectedError as exc:
            msg = str(exc)
            if any(frag in msg for frag in _BENIGN_BREAKEVEN_FRAGMENTS):
                # Broker has already replaced the leg — flag it as moved so
                # PositionManager doesn't keep re-emitting the breakeven
                # action every cycle (today's COIN log: 6 retries before
                # the position even time-stopped).
                if pos is not None:
                    pos.breakeven_moved = True
                self.logger.warning("BREAKEVEN_SKIPPED symbol=%s stop_leg=%s reason=%s",
                                    a.symbol, stop_leg, msg)
                return
            self.logger.error("BREAKEVEN_REPLACE_FAILED symbol=%s stop_leg=%s error=%s",
                              a.symbol, stop_leg, exc, exc_info=True)
        except Exception as exc:
            self.logger.error("BREAKEVEN_REPLACE_FAILED symbol=%s stop_leg=%s error=%s",
                              a.symbol, stop_leg, exc, exc_info=True)
```

(`pos is not None` guards mirror the existing `stop_leg = pos.stop_order_id if pos else None` defensive pattern.)

- [ ] **Step 4: Run test to verify it passes**

```bash
pytest tests/test_order_executor.py -v -k "breakeven"
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add broker/order_executor.py tests/test_order_executor.py
git commit -m "fix(order_executor): flip breakeven_moved on success and on already-replaced rejection"
```

---

## Task 7: Reconciler emits `reconciler_close_fill_unmatched` event

**Files:**
- Modify: `reconciler/fills.py:161-185` (the `_EXIT_ROLES` branch in `apply_tagged_fill`)
- Test: `tests/test_reconciler_fills.py` (existing)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_reconciler_fills.py`:

```python
def test_apply_tagged_fill_exit_no_open_row_emits_unmatched_event(
    session, store, sample_strategy_id,
):
    # Build a close-role COID for a setup/symbol with no open MySQL row
    coid = make_client_order_id(
        "vwap_wave_equity", "price_discovery", "COIN", Role.EXIT,
    )
    fill = {
        "id": "alpaca-close-1",
        "client_order_id": coid,
        "symbol": "COIN",
        "side": "buy",
        "filled_avg_price": "175.10",
        "filled_qty": "1",
        "asset_class": "us_equity",
    }
    apply_tagged_fill(session, fill, store, cycle_asset_class="equity")
    session.commit()

    events = session.query(EventRow).filter(
        EventRow.type == "reconciler_close_fill_unmatched"
    ).all()
    assert len(events) == 1
    assert events[0].symbol == "COIN"
    payload = events[0].payload
    assert payload["client_order_id"] == coid
    assert payload["setup"] == "price_discovery"
    assert payload["role"] == "X"
    assert payload["alpaca_id"] == "alpaca-close-1"
```

(Adapt fixture names — `session`, `store`, `sample_strategy_id`, `EventRow` — to what `tests/test_reconciler_fills.py` already uses. Check the file's existing tests first.)

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/test_reconciler_fills.py -v -k "unmatched"
```

Expected: FAIL — no event of type `reconciler_close_fill_unmatched` is emitted.

- [ ] **Step 3: Update `apply_tagged_fill`**

In `reconciler/fills.py`, modify the `_EXIT_ROLES` branch (around lines 161-185):

```python
    if role in _EXIT_ROLES:
        open_row = store.find_open_position_by_setup(strategy_id, symbol, setup)
        if open_row is None:
            emit_event(
                session,
                type="reconciler_close_fill_unmatched",
                strategy_id=strategy_id,
                symbol=symbol,
                asset_class=cycle_asset_class,
                payload={
                    "alpaca_id": fill.get("id"),
                    "client_order_id": coid,
                    "setup": setup,
                    "role": role,
                },
            )
            return  # idempotent noop preserved
        # ... rest unchanged
```

- [ ] **Step 4: Run test to verify it passes**

```bash
pytest tests/test_reconciler_fills.py -v
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add reconciler/fills.py tests/test_reconciler_fills.py
git commit -m "feat(reconciler): emit reconciler_close_fill_unmatched event for visibility"
```

---

## Task 8: Audit script `scripts/audit_phantom_close_stacks.py`

**Files:**
- Create: `scripts/audit_phantom_close_stacks.py`
- Create: `tests/test_audit_phantom_close_stacks.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_audit_phantom_close_stacks.py`:

```python
"""Audit script detects MySQL/broker drift across both Alpaca accounts.

Detection rule: aggregate MySQL open rows by (symbol, asset_class) into a
signed qty (long=+, short=-). Compare to the broker's signed qty (which is
already aggregated by Alpaca per symbol). Drift exists when they differ.
This handles the multi-setup case where two setups can share a symbol on
the same account.
"""
from __future__ import annotations
from datetime import datetime, timezone
from io import StringIO
from unittest.mock import MagicMock

from scripts.audit_phantom_close_stacks import (
    aggregate_mysql_signed_qty,
    broker_signed_qty,
    detect_drift,
    format_report,
    DriftRow,
)


def test_aggregate_mysql_signed_qty_sums_setups():
    rows = [
        {"symbol": "COIN", "asset_class": "equity",
         "side": "short", "qty": 1.0, "setup_name": "price_discovery",
         "id": 1, "opened_at": "2026-06-02T15:00:00Z",
         "client_order_id": "coid-1"},
        {"symbol": "COIN", "asset_class": "equity",
         "side": "short", "qty": 2.0, "setup_name": "fade_extreme",
         "id": 2, "opened_at": "2026-06-02T15:30:00Z",
         "client_order_id": "coid-2"},
    ]
    result = aggregate_mysql_signed_qty(rows)
    assert result[("COIN", "equity")]["signed_qty"] == -3.0
    assert len(result[("COIN", "equity")]["rows"]) == 2


def test_aggregate_mysql_signed_qty_long_short_offset():
    rows = [
        {"symbol": "COIN", "asset_class": "equity",
         "side": "long", "qty": 5.0, "setup_name": "a",
         "id": 1, "opened_at": "2026-06-02T15:00:00Z", "client_order_id": ""},
        {"symbol": "COIN", "asset_class": "equity",
         "side": "short", "qty": 2.0, "setup_name": "b",
         "id": 2, "opened_at": "2026-06-02T15:30:00Z", "client_order_id": ""},
    ]
    result = aggregate_mysql_signed_qty(rows)
    assert result[("COIN", "equity")]["signed_qty"] == 3.0


def test_broker_signed_qty_long():
    pos = {"symbol": "COIN", "qty": "22", "side": "long",
           "asset_class": "us_equity", "avg_entry_price": "174.30"}
    assert broker_signed_qty(pos) == 22.0


def test_broker_signed_qty_short():
    pos = {"symbol": "COIN", "qty": "-22", "side": "short",
           "asset_class": "us_equity", "avg_entry_price": "174.30"}
    assert broker_signed_qty(pos) == -22.0


def test_detect_drift_today_coin_incident():
    """Today's actual incident: MySQL has 1 short, broker has 22 long."""
    mysql_rows = [{
        "symbol": "COIN", "asset_class": "equity",
        "side": "short", "qty": 1.0, "setup_name": "price_discovery",
        "id": 1234, "opened_at": "2026-06-02T15:00:00Z",
        "client_order_id": "coid-stuck",
    }]
    broker_positions = [{
        "symbol": "COIN", "qty": "22", "side": "long",
        "asset_class": "us_equity", "avg_entry_price": "174.30",
    }]
    drifts = detect_drift(mysql_rows, broker_positions)
    assert len(drifts) == 1
    d = drifts[0]
    assert d.symbol == "COIN"
    assert d.asset_class == "equity"
    assert d.mysql_signed_qty == -1.0
    assert d.broker_signed_qty == 22.0
    assert d.delta == 23.0
    assert d.suggested_flatten_side == "sell"
    assert d.suggested_flatten_qty == 22.0


def test_detect_drift_no_drift_returns_empty():
    mysql_rows = [{
        "symbol": "COIN", "asset_class": "equity",
        "side": "short", "qty": 1.0, "setup_name": "price_discovery",
        "id": 1, "opened_at": "2026-06-02T15:00:00Z",
        "client_order_id": "ok",
    }]
    broker_positions = [{
        "symbol": "COIN", "qty": "-1", "side": "short",
        "asset_class": "us_equity", "avg_entry_price": "174.07",
    }]
    drifts = detect_drift(mysql_rows, broker_positions)
    assert drifts == []


def test_detect_drift_mysql_only_no_broker_position():
    """MySQL has an open row but the broker shows no position. Also drift."""
    mysql_rows = [{
        "symbol": "COIN", "asset_class": "equity",
        "side": "short", "qty": 1.0, "setup_name": "price_discovery",
        "id": 1, "opened_at": "2026-06-02T15:00:00Z",
        "client_order_id": "ok",
    }]
    drifts = detect_drift(mysql_rows, broker_positions=[])
    assert len(drifts) == 1
    assert drifts[0].broker_signed_qty == 0.0
    assert drifts[0].mysql_signed_qty == -1.0


def test_detect_drift_broker_only_no_mysql_row():
    drifts = detect_drift(
        mysql_rows=[],
        broker_positions=[{
            "symbol": "GHOST", "qty": "5", "side": "long",
            "asset_class": "us_equity", "avg_entry_price": "10.00",
        }],
    )
    assert len(drifts) == 1
    assert drifts[0].mysql_signed_qty == 0.0
    assert drifts[0].broker_signed_qty == 5.0


def test_format_report_includes_suggested_flatten():
    drift = DriftRow(
        symbol="COIN", asset_class="equity",
        mysql_signed_qty=-1.0, broker_signed_qty=22.0, delta=23.0,
        mysql_rows=[{
            "id": 1234, "setup_name": "price_discovery",
            "side": "short", "qty": 1.0,
            "opened_at": "2026-06-02T15:00:00Z",
            "client_order_id": "coid-stuck",
        }],
        broker_position={
            "symbol": "COIN", "qty": "22", "side": "long",
            "avg_entry_price": "174.30",
        },
        suggested_flatten_side="sell",
        suggested_flatten_qty=22.0,
    )
    out = format_report([drift])
    assert "DRIFT symbol=COIN" in out
    assert "id=1234 setup=price_discovery" in out
    assert "qty=22" in out
    assert "suggested_manual_flatten" in out
    assert "side=sell qty=22" in out


def test_format_report_no_drift_says_so():
    out = format_report([])
    assert "no drift" in out.lower()
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/test_audit_phantom_close_stacks.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'scripts.audit_phantom_close_stacks'`.

- [ ] **Step 3: Implement the script**

Create `scripts/audit_phantom_close_stacks.py`:

```python
"""Read-only audit of MySQL/broker position drift across both Alpaca accounts.

Triggered by the 2026-06-02 COIN incident: trader-vwap-wave-equity stacked
22 long broker positions on COIN while MySQL showed 1 short open. This
script aggregates MySQL open rows by (symbol, asset_class) into a signed
qty and compares to the broker's per-symbol aggregated signed qty,
flagging any mismatch.

Usage:
    python -m scripts.audit_phantom_close_stacks

Output: human-readable per-symbol report. Exit code is always 0 — this is
a report, not a CI gate. No mutations.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from typing import Any, Iterable

from broker.alpaca_router import AlpacaRouter
from state.mysql_store import MySQLStore, PositionRow


@dataclass
class DriftRow:
    symbol: str
    asset_class: str
    mysql_signed_qty: float
    broker_signed_qty: float
    delta: float  # broker_signed_qty - mysql_signed_qty
    mysql_rows: list[dict] = field(default_factory=list)
    broker_position: dict | None = None
    suggested_flatten_side: str = ""  # 'sell' or 'buy'
    suggested_flatten_qty: float = 0.0


def aggregate_mysql_signed_qty(
    rows: Iterable[dict],
) -> dict[tuple[str, str], dict]:
    """Group MySQL open rows by (symbol, asset_class) and compute signed qty.

    Returns a dict keyed by (symbol, asset_class) with:
      - signed_qty: sum(qty if side=='long' else -qty)
      - rows: list of the original dicts (for the report's per-row detail)
    """
    out: dict[tuple[str, str], dict] = {}
    for r in rows:
        key = (r["symbol"], r["asset_class"])
        bucket = out.setdefault(key, {"signed_qty": 0.0, "rows": []})
        sign = 1.0 if r["side"] == "long" else -1.0
        bucket["signed_qty"] += sign * float(r["qty"])
        bucket["rows"].append(r)
    return out


def broker_signed_qty(pos: dict) -> float:
    """Alpaca returns position qty as a string; sign is in the side field
    (the qty string itself can be negative for shorts on equity, but is
    always positive on crypto). Use side as the source of truth."""
    qty = abs(float(pos["qty"]))
    return qty if pos["side"] == "long" else -qty


def _broker_asset_class(pos: dict) -> str:
    """Alpaca emits 'us_equity' / 'crypto' — normalize to our internal
    'equity' / 'crypto' values used in MySQL.asset_class."""
    ac = pos.get("asset_class") or ""
    return "crypto" if ac == "crypto" else "equity"


def detect_drift(
    mysql_rows: list[dict],
    broker_positions: list[dict],
) -> list[DriftRow]:
    """Return a DriftRow per (symbol, asset_class) where MySQL signed qty
    disagrees with broker signed qty. Includes asymmetric cases:
      - mysql_only: MySQL has an open row, broker has no position
      - broker_only: broker has a position, no matching MySQL row
    """
    mysql_agg = aggregate_mysql_signed_qty(mysql_rows)
    broker_by_key: dict[tuple[str, str], dict] = {}
    for p in broker_positions:
        key = (p["symbol"], _broker_asset_class(p))
        # Multiple rows shouldn't happen (broker aggregates per symbol)
        # but if they do, sum.
        prev = broker_by_key.get(key)
        if prev is None:
            broker_by_key[key] = p
        else:
            # Synthesize an aggregated dict
            prev_qty = broker_signed_qty(prev)
            this_qty = broker_signed_qty(p)
            total = prev_qty + this_qty
            broker_by_key[key] = {
                **prev,
                "qty": str(abs(total)),
                "side": "long" if total >= 0 else "short",
            }

    out: list[DriftRow] = []
    all_keys = set(mysql_agg.keys()) | set(broker_by_key.keys())
    for key in sorted(all_keys):
        symbol, asset_class = key
        mysql_q = mysql_agg.get(key, {}).get("signed_qty", 0.0)
        bp = broker_by_key.get(key)
        broker_q = broker_signed_qty(bp) if bp else 0.0
        if mysql_q == broker_q:
            continue
        delta = broker_q - mysql_q
        # Flatten direction: if broker is long-heavy vs MySQL, sell to bring
        # the broker's net to mysql's. (We don't suggest closing all the way
        # to zero — only the excess. Operator decides whether MySQL is right.)
        suggested_qty = abs(delta)
        suggested_side = "sell" if delta > 0 else "buy"
        out.append(DriftRow(
            symbol=symbol,
            asset_class=asset_class,
            mysql_signed_qty=mysql_q,
            broker_signed_qty=broker_q,
            delta=delta,
            mysql_rows=mysql_agg.get(key, {}).get("rows", []),
            broker_position=bp,
            suggested_flatten_side=suggested_side,
            suggested_flatten_qty=suggested_qty,
        ))
    return out


def format_report(drifts: list[DriftRow]) -> str:
    if not drifts:
        return "no drift detected — MySQL and broker agree on every symbol."
    lines: list[str] = []
    for d in drifts:
        lines.append(f"DRIFT symbol={d.symbol} asset_class={d.asset_class}")
        lines.append(
            f"  mysql_open: {len(d.mysql_rows)} row(s) — "
            f"net signed qty = {d.mysql_signed_qty:+g}"
        )
        for r in d.mysql_rows:
            lines.append(
                f"    id={r['id']} setup={r['setup_name']} "
                f"side={r['side']} qty={r['qty']} "
                f"opened_at={r['opened_at']} "
                f"coid={r.get('client_order_id') or '<none>'}"
            )
        if d.broker_position:
            bp = d.broker_position
            lines.append(
                f"  broker: side={bp['side']} qty={bp['qty']} "
                f"avg_entry={bp.get('avg_entry_price', '?')} "
                f"— net signed qty = {d.broker_signed_qty:+g}"
            )
        else:
            lines.append("  broker: no matching position")
        lines.append(f"  drift: broker_signed - mysql_signed = {d.delta:+g}")
        lines.append(
            f"  suggested_manual_flatten: side={d.suggested_flatten_side} "
            f"qty={d.suggested_flatten_qty:g}"
        )
        lines.append("")
    return "\n".join(lines)


def _fetch_mysql_rows() -> list[dict]:
    """Pull every open PositionRow across all strategies on this MySQL.

    MySQLStore is per-strategy, so we go directly through the engine to
    sweep all strategies in one pass.
    """
    from sqlalchemy.orm import Session
    from state.mysql_store import _build_engine  # private helper; OK for a script

    engine = _build_engine()
    with Session(engine) as session:
        rows = session.query(PositionRow).filter(
            PositionRow.status == "open",
        ).all()
        return [{
            "id": r.id,
            "symbol": r.symbol,
            "asset_class": r.asset_class,
            "side": r.side,
            "qty": float(r.qty),
            "setup_name": r.setup_name,
            "opened_at": r.opened_at.isoformat() if r.opened_at else "",
            "client_order_id": r.client_order_id or "",
        } for r in rows]


def main() -> int:
    try:
        mysql_rows = _fetch_mysql_rows()
    except Exception as exc:
        print(f"ERROR fetching MySQL rows: {exc}", file=sys.stderr)
        return 0  # report-only, never fail

    try:
        router = AlpacaRouter()
        broker_positions = router.get_positions()
    except Exception as exc:
        print(f"ERROR fetching broker positions: {exc}", file=sys.stderr)
        return 0

    drifts = detect_drift(mysql_rows, broker_positions)
    print(format_report(drifts))
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

If `state/mysql_store.py` doesn't expose a `_build_engine` helper, search the file for how `MySQLStore` constructs its engine in `__init__` and replicate the connection-string assembly inline in `_fetch_mysql_rows`. Do NOT add a public helper just for this script — keep the script self-contained.

- [ ] **Step 4: Run test to verify it passes**

```bash
pytest tests/test_audit_phantom_close_stacks.py -v
```

Expected: all nine tests pass.

- [ ] **Step 5: Smoke-test the CLI (read-only, harmless)**

```bash
python -m scripts.audit_phantom_close_stacks
```

Expected: prints the drift report (or "no drift detected") and exits 0. If MySQL or Alpaca creds aren't available locally, prints an error and still exits 0.

- [ ] **Step 6: Commit**

```bash
git add scripts/audit_phantom_close_stacks.py tests/test_audit_phantom_close_stacks.py
git commit -m "feat(scripts): read-only audit of MySQL/broker position drift across accounts"
```

---

## Task 9: Final regression sweep

**Files:** none modified — this is a verification task only.

- [ ] **Step 1: Run the full test suite**

```bash
pytest -v
```

Expected: all green. New target: ≥229 tests (215 previously + ~14 new).

- [ ] **Step 2: Verify no `setup="_unknown"` remains in non-test code**

```bash
grep -rn '"_unknown"' --include="*.py" | grep -v tests/
```

Expected: only `state/operator_close.py` may legitimately use a synthetic setup (`"cleanslate"`) — and `state/strategy_close_all.py` may use `"disable"` — those are operator-orphan-flatten paths where there's no source MySQL row to match. They're fine; the new `RECONCILER_CLOSE_FILL_UNMATCHED` event will surface them as observability rather than silent noops.

If `broker/order_executor.py` still has `"_unknown"`, the fix is incomplete — go back to Task 4.

- [ ] **Step 3: Verify the gate ordering hasn't regressed**

Read `core/position_manager.py` `on_bar` and confirm the loop body order is:
1. `_confirm_fill` gate
2. `exit_submitted` gate
3. `_check_position`

- [ ] **Step 4: Run the audit script against staging or paper account if available**

```bash
python -m scripts.audit_phantom_close_stacks
```

Capture the output. Today's COIN drift on the equity account should appear; expected output looks like:

```
DRIFT symbol=COIN asset_class=equity
  mysql_open: 1 row(s) — net signed qty = -1
    id=<id> setup=price_discovery side=short qty=1.0 opened_at=2026-06-02T15:00:00Z coid=<...>
  broker: side=long qty=22 avg_entry=174.30 — net signed qty = +22
  drift: broker_signed - mysql_signed = +23
  suggested_manual_flatten: side=sell qty=22
```

(After operator manually flattens COIN per the deploy plan, re-running should print "no drift detected".)

---

## Summary of files at the end

| File | Change |
|------|--------|
| `state/position_book.py` | +1 field `exit_submitted` on `OpenPosition` |
| `state/mysql_store.py` | +1 column, +1 migration, +1 helper, +1 round-trip pair |
| `core/position_manager.py` | +1 gate in `on_bar` |
| `broker/order_executor.py` | `close_position` requires `setup`; `handle_actions` flips flag on every exit branch; `_mark_exit_submitted` helper; `_move_equity_stop_to_breakeven` flips `breakeven_moved` |
| `reconciler/fills.py` | Emit `reconciler_close_fill_unmatched` event |
| `scheduler/gap_and_go_loop.py` | Pass `setup=pos.setup` to `close_position` |
| `scripts/audit_phantom_close_stacks.py` | New |
| `tests/test_position_book.py` | +2 tests |
| `tests/test_mysql_store_coid.py` | +4 tests |
| `tests/test_position_manager_exit_gate.py` | New, 4 tests |
| `tests/test_close_coid_setup.py` | New, 2 tests |
| `tests/test_order_executor_exit_idempotency.py` | New, 5 tests |
| `tests/test_order_executor.py` | +2 breakeven tests |
| `tests/test_reconciler_fills.py` | +1 test |
| `tests/test_audit_phantom_close_stacks.py` | New, 9 tests |
| `tests/test_order_executor_actions.py`, `tests/test_safe_close.py`, `tests/test_post_open_attach.py`, `tests/test_gap_and_go_loop.py` | Updated for new `setup=` arg |

## Self-review

**Spec coverage:**
- Component 1 (`OpenPosition.exit_submitted`) → Task 1 ✓
- Component 2 (`PositionRow.exit_submitted` + migration) → Task 2 ✓
- Component 3 (`mark_exit_submitted`) → Task 2 ✓
- Component 4 (`PositionManager.on_bar` gate) → Task 3 ✓
- Component 5 (`close_position(setup=)`) → Task 4 ✓
- Component 6 (`handle_actions` flag flip) → Task 5 ✓
- Component 7 (`_move_equity_stop_to_breakeven`) → Task 6 ✓
- Component 8 (`RECONCILER_CLOSE_FILL_UNMATCHED`) → Task 7 ✓
- Component 9 (audit script) → Task 8 ✓
- Spec testing section → all tests in Tasks 1–8, regression in Task 9 ✓

**Placeholder scan:** none.

**Type consistency:** `mark_exit_submitted` uses `(strategy_id, symbol, setup_name)` matching `mark_fill_confirmed`. `close_position` uses `setup` (not `setup_name`) matching `make_client_order_id`'s positional API. `_mark_exit_submitted` (private on executor) takes `(symbol, setup)` consistent with `PositionAction.setup`.
