# Engine Exit Idempotency + Real-Setup Close COID

**Date:** 2026-06-02
**Status:** Draft → pending implementation
**Triggering incident:** trader-vwap-wave-equity stacked 22 long broker positions on COIN while MySQL showed 1 short open. Caused by an unbounded `time_stop` re-fire loop combined with an unmatched close-fill COID.

## Problem

Two bugs on the engine exit path interact to produce stacked phantom broker positions:

1. **No exit idempotency in `PositionManager`.** `on_bar` re-evaluates every position in the book on every bar. PR #92 added a `fill_confirmed` gate for entries, but no equivalent gate for exits. After a `time_stop` action is emitted and `close_position` submits a market close, the position is still in the book on the next cycle — `bars_held` keeps growing, `_check_position` re-emits `time_stop`, and `OrderExecutor.handle_actions` submits another market close. Repeats every cycle.
2. **Close COID uses `setup="_unknown"`** (`broker/order_executor.py:303`). The exit COID can therefore never match the open MySQL row at the reconciler. `reconciler/fills.py:apply_tagged_fill` calls `find_open_position_by_setup(strategy_id, symbol, "unknown")`, gets `None`, and returns silently as an idempotent noop. The MySQL row stays open across every reconciler cycle, so `book.replace_from(mysql.load_open_positions())` keeps reloading it, and the loop in (1) is unbounded.

Today's COIN log: 22 `TIME_STOP` lines between 16:05 and 18:30, 7 `BRACKET_EXIT` lines at the bracket stop level, 6 `BREAKEVEN_REPLACE_FAILED ... already replaced` lines from a parallel idempotency hole in the breakeven path. Eventually `SAFE_CLOSE_FAILED ... insufficient buying power` and unrelated `ORDER_REJECTED_DTBP` on RSI as collateral damage.

This is a P0. Cross-strategy collateral damage is already happening (RSI rejected because vwap_wave ate the equity account's DTBP).

## Goals

1. Fire exactly one broker close per logical exit per position.
2. Make the reconciler match that close fill back to the right MySQL row.
3. Make the same class of bug impossible elsewhere (breakeven replace, safe_close, operator close).
4. Provide a read-only audit script so we can detect today's mess and any future drift across both Alpaca accounts.

## Non-goals

- Backfilling historical phantom rows. Operators audit via the SQL in the PR #92 commit message.
- Manually flattening today's COIN stack. Operator does that at the broker before deploy.
- Auto-flattening detected drift. The cleanup script is detect-only by design (auto-flatten is too risky given today's bug class).
- Changing the reconciler's idempotency semantics. `find_open_position_by_setup` returning `None` on duplicate-fill is correct.

## Architecture

### Mental model

`fill_confirmed` from PR #92 gates the *entry* side: skip `on_bar` until the broker confirms the entry filled. We add a symmetric `exit_submitted` flag that gates the *exit* side: skip `on_bar` once the engine has submitted a close, regardless of whether the broker has filled it yet. The position leaves the book when the reconciler closes the MySQL row from the broker's actual close fill, and the next cycle's `book.replace_from(mysql.load_open_positions())` drops it.

This is symmetric to PR #92 — flag on `OpenPosition`, column on `PositionRow`, persistence helper on `MySQLStore` — opposite gate direction (entry: skip until True; exit: skip after True).

### Components

#### 1. `OpenPosition.exit_submitted: bool = False` — `state/position_book.py`

Mirrors `fill_confirmed`. Default `False` for new positions. Round-tripped through `_pos_to_dict` / `_row_to_pos`.

#### 2. `PositionRow.exit_submitted` column — `state/mysql_store.py`

```python
exit_submitted: Mapped[bool] = mapped_column(Boolean, default=False)
```

Idempotent ALTER in `_run_migrations`:

```sql
ALTER TABLE positions ADD COLUMN exit_submitted TINYINT(1) NOT NULL DEFAULT 0
```

Default `0` for existing rows. A row mid-exit at deploy time will get one re-evaluation cycle: if the engine still wants to exit, it submits one close (now stamped with the real-setup COID) and flips the flag. This is acceptable — it's a single replay, bounded.

#### 3. `MySQLStore.mark_exit_submitted` — `state/mysql_store.py`

Analog of `mark_fill_confirmed`:

```python
def mark_exit_submitted(
    self, *, strategy_id: int, symbol: str, setup: str,
) -> None:
    """Flip an open position's exit_submitted flag to True.

    Idempotent — re-applies cleanly if the row is already True.
    """
```

#### 4. `PositionManager.on_bar` exit gate — `core/position_manager.py`

After the existing `_confirm_fill` gate, add:

```python
if pos.exit_submitted:
    # Engine has already submitted a broker close for this position.
    # Defer everything — bars_held is NOT incremented, and no further
    # exit actions are emitted. The position leaves the book when the
    # reconciler closes the MySQL row from the broker's close fill.
    continue
```

`bars_held` is not bumped (consistent with the entry gate's behavior).

#### 5. `OrderExecutor.close_position(..., setup: str)` — `broker/order_executor.py`

`setup` becomes a required positional/keyword argument. The COID is built from the real setup name:

```python
exit_coid = make_client_order_id(
    self.strategy_name, setup, symbol, Role.EXIT,
)
```

Caller updates:

- `OrderExecutor.handle_actions` — equity `time_stop` and crypto `stop|target|time_stop` branches both pass `a.setup` (already on the `PositionAction`).
- `broker/safe_close.py` — callers must pass setup. Audit each call site (`scheduler/loop.py`, `reconciler/main.py` orphan-close path, `state/strategy_close_all.py`, `state/operator_close.py`).
- `state/strategy_close_all.py` — already iterates over book entries, has setup available.
- `state/operator_close.py` — same.

A close path with no known setup is a code smell. If any call site genuinely can't supply one (operator close on a broker-only orphan with no MySQL row), it stays as `"_unknown"` and the reconciler logs `RECONCILER_CLOSE_FILL_UNMATCHED` — see component 8.

#### 6. `OrderExecutor.handle_actions` orchestrates the flip

For each branch that submits a broker close:

**Equity time_stop:**
```python
if a.kind == "time_stop":
    if parent_order_id:
        try:
            self.client.cancel_order(parent_order_id)
        except Exception as exc:
            self.logger.warning(
                "CANCEL_FAILED_DURING_TIME_STOP symbol=%s order_id=%s error=%s "
                "— treating parent as already terminal, proceeding with close",
                a.symbol, parent_order_id, exc,
            )
            # Don't re-raise. A failed cancel here usually means the parent
            # is already canceled or filled; submitting the close is still
            # the right action and exit_submitted will be flipped below so
            # we don't loop.
    self.close_position(a.symbol, a.side, a.qty, setup=a.setup,
                        asset_class="equity")
    self._mark_exit_submitted(a.symbol, a.setup)
    self.logger.info("TIME_STOP symbol=%s side=%s qty=%s setup=%s",
                     a.symbol, a.side, a.qty, a.setup)
    continue
```

**Equity stop/target (`BRACKET_EXIT`):** the broker's bracket OCO is firing server-side. Flip the flag so the engine doesn't keep re-emitting on subsequent bars while the OCO leg is in-flight:

```python
if a.kind in ("stop", "target"):
    self._mark_exit_submitted(a.symbol, a.setup)
    self.logger.info("BRACKET_EXIT symbol=%s kind=%s price=%.4f setup=%s",
                     a.symbol, a.kind, a.price, a.setup)
    continue
```

**Crypto stop/target/time_stop:** same shape as equity time_stop — close + flip.

`_mark_exit_submitted(symbol, setup)` is a private method on `OrderExecutor` that:
1. Looks up the position in `self.book` and sets `pos.exit_submitted = True`.
2. Calls `self.mysql_store.mark_exit_submitted(strategy_id=..., symbol=..., setup=...)` if `mysql_store` is set.

If the close submission itself raises (`safe_close_failed` etc.), the flag is **not** flipped — the next cycle retries. This is the existing behavior; we just don't lock ourselves out of retry on real failures.

#### 7. `_move_equity_stop_to_breakeven` secondary fix

The `BREAKEVEN_REPLACE_FAILED ... already replaced` loop in today's COIN log is the same disease. The existing `breakeven_moved` field on `OpenPosition` is the right gate; `PositionManager._check_position` already uses it, but `_move_equity_stop_to_breakeven` doesn't set it on success. Add:

```python
self.client.replace_order(stop_leg, stop_price=a.price)
pos.breakeven_moved = True  # ← NEW
self.logger.info("BREAKEVEN_REPLACED symbol=%s ...", ...)
```

And handle the "already replaced" rejection from the broker by also setting `breakeven_moved = True` (the broker has confirmed the move happened):

```python
except OrderRejectedError as exc:
    msg = str(exc)
    if any(frag in msg for frag in _BENIGN_BREAKEVEN_FRAGMENTS):
        pos.breakeven_moved = True  # ← NEW
        self.logger.warning("BREAKEVEN_SKIPPED symbol=%s ...", ...)
        return
```

In-memory only (`breakeven_moved` is already in-memory; no migration). On restart the position re-evaluates and either replaces successfully or hits the benign-fragment branch and flips.

#### 8. `RECONCILER_CLOSE_FILL_UNMATCHED` log line — `reconciler/fills.py`

In `apply_tagged_fill`, the exit-role branch:

```python
if role in _EXIT_ROLES:
    open_row = store.find_open_position_by_setup(strategy_id, symbol, setup)
    if open_row is None:
        emit_event(
            session,
            type="reconciler_close_fill_unmatched",  # ← NEW
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
        return  # idempotent noop (unchanged)
```

No behavioral change — this is observability so the next time a setup-mismatch bug class shows up, it's loud.

#### 9. Audit script — `scripts/audit_phantom_close_stacks.py`

Read-only CLI. Reads MySQL + both Alpaca accounts via `AlpacaRouter`. For each broker position with `qty > 0`, looks up the matching MySQL open row(s) on `(symbol, asset_class)` and reports:

```
DRIFT symbol=COIN asset_class=equity
  mysql_open: 1 row(s) — net signed qty = -1 (short)
    id=1234 setup=price_discovery side=short qty=1 opened_at=2026-06-02T15:00 coid=...
  broker:
    side=long qty=22 avg_entry=174.30 — net signed qty = +22
  drift: broker_signed - mysql_signed = +23
  suggested_manual_flatten:
    side=sell qty=22  # bring broker net to 0, leaving the engine to manage the MySQL short
```

Detection rule: compute `mysql_signed_qty = sum(qty if side=="long" else -qty for row in mysql_open_rows)` (aggregated across all setups for the same symbol+asset_class — the broker only sees one combined position per symbol). Compare to `broker_signed_qty = qty if broker_side=="long" else -qty`. Drift exists when `mysql_signed_qty != broker_signed_qty`. This handles the multi-setup case correctly (e.g. vwap_wave + vwap_bands both holding COIN long sums in MySQL to match the broker's aggregated long).

Exit code 0 always — this is a report, not a check.

No mutations. No side effects. Operator decides what to do with the output.

## Error handling & edge cases

| Scenario | Behavior |
|----------|----------|
| Cancel raises during time_stop | Log warning, proceed with close, flip flag (Q3 decision) |
| `close_position` raises | Don't flip flag, next cycle retries |
| Restart mid-exit, exit_submitted persisted | Engine reloads with flag=True, skips position, reconciler eventually closes MySQL from broker fill |
| Restart mid-exit, exit_submitted=False (legacy row) | One re-evaluation cycle, engine submits one close with real-setup COID, flips flag, reconciler closes |
| Reconciler can't find open row by setup | `RECONCILER_CLOSE_FILL_UNMATCHED` event emitted, idempotent noop preserved |
| In-flight position pre-fix with COID `_unknown` already in flight | Reconciler logs unmatched event when fill arrives. Operator handles via cleanup script + manual close. The pre-fix population is bounded (today's COIN, possibly a handful of others). |
| BRACKET_EXIT fires, then broker fill never arrives (rare) | exit_submitted=True locks the engine out; reconciler's `mysql_only` auto-resolve from PR #91 cleans up the row eventually. |

## Testing

### New tests

- **`tests/test_position_manager_exit_gate.py`** — `on_bar` skips positions with `exit_submitted=True`; `bars_held` does not increment for skipped positions; gate runs after `_confirm_fill` (a position that's both unconfirmed-fill and exit-submitted is skipped at the fill gate first).
- **`tests/test_order_executor_exit_idempotency.py`** —
  - `handle_actions` time_stop: success path flips flag in book + calls `mark_exit_submitted`; cancel-failure path flips and proceeds; close-submission failure does NOT flip.
  - `handle_actions` BRACKET_EXIT (stop/target): flips flag without submitting a close.
  - Crypto path: flips flag after close.
  - Repeated handle_actions calls for the same position emit exactly one close (the first; subsequent are gated by the engine, not re-tested here — covered in the PM gate test).
- **`tests/test_close_coid_setup.py`** — close COID parses to the real setup name, not `unknown`. End-to-end with `apply_tagged_fill` matching the open row.
- **`tests/test_audit_phantom_close_stacks.py`** — fixture: MySQL has one short qty=1, broker has long qty=22 same symbol; report includes the row, suggested flatten qty=22 sell, exit code 0.

### Updated tests

- `tests/test_record_exits_to_ledger.py` — call sites with new `setup` arg on `close_position`.
- `tests/test_order_executor_actions.py` — same.
- `tests/test_safe_close.py` — same.
- `tests/test_post_open_attach.py` — audit, update if it touches `close_position`.

### Regression bar

The existing 215-test reconciler+scheduler+PM suite must stay green. New count target: ≥229 (215 + ~14 new across the four new test files).

## Migration & rollout

1. **Operator action before merge:** flatten COIN at the broker for trader-vwap-wave-equity. Confirm one open MySQL `price_discovery` short remains.
2. **Schema migration:** idempotent ALTER on `positions` adds `exit_submitted TINYINT(1) NOT NULL DEFAULT 0`. Runs at trader startup; safe to re-apply.
3. **Deploy order:** rebuild trader images, deploy. Reconciler doesn't need to change beyond the new event type, which is additive.
4. **Post-deploy verification:**
   - `scripts/audit_phantom_close_stacks.py` shows no drift on either account.
   - Logs show `TIME_STOP` followed by `mark_exit_submitted`, no repeated `TIME_STOP` for the same `(symbol, setup)` within a session.
   - Logs show `BREAKEVEN_REPLACED` followed by no `BREAKEVEN_REPLACE_FAILED ... already replaced` retries.

## Files touched

- `state/position_book.py` — `OpenPosition.exit_submitted`
- `state/mysql_store.py` — `PositionRow.exit_submitted`, migration, `mark_exit_submitted`, `_pos_to_dict`/`_row_to_pos` round-trip
- `core/position_manager.py` — exit gate in `on_bar`
- `broker/order_executor.py` — `close_position(setup=...)`, `handle_actions` flag flips, `_mark_exit_submitted` helper, `_move_equity_stop_to_breakeven` idempotency
- `broker/safe_close.py` and any caller passing setup through (audit only — likely already has it)
- `state/strategy_close_all.py`, `state/operator_close.py` — pass setup to `close_position`
- `reconciler/fills.py` — `RECONCILER_CLOSE_FILL_UNMATCHED` event
- `scripts/audit_phantom_close_stacks.py` — new
- `tests/test_position_manager_exit_gate.py` — new
- `tests/test_order_executor_exit_idempotency.py` — new
- `tests/test_close_coid_setup.py` — new
- `tests/test_audit_phantom_close_stacks.py` — new
- `tests/test_record_exits_to_ledger.py`, `tests/test_order_executor_actions.py`, `tests/test_safe_close.py`, `tests/test_post_open_attach.py` — updated for new `setup` arg

## Open questions

None remaining. Q1 (persist), Q2 (detect-only), Q3 (cancel-fail proceeds + flip) all resolved during brainstorming.
