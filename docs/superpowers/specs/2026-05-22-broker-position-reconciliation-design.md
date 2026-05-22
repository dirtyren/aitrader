# Broker Position Reconciliation

**Status:** Design approved, awaiting plan
**Date:** 2026-05-22
**Author:** brainstorming session

## Problem

The trader logs `open_positions=0` while Alpaca holds 4 open positions. The local `PositionBook` is initialized empty at startup (`main.py:281`) and is never reconciled against the broker. Positions surviving from a prior process run, opened manually in the Alpaca UI, or closed server-side by a bracket child while the trader was sleeping between bars, are invisible to:

- `book.count()` reported in `CYCLE_DONE` (under-reports)
- `book.aggregate_open_risk_usd()` consumed by `RiskManager` (under-reports → can over-allocate new entries)
- `PositionManager` lifecycle (orphaned positions get no breakeven, no time_stop, no exit on bracket fire)
- Dashboard snapshot

## Goal

Make the in-memory `PositionBook` match Alpaca's reality at all times: at startup, and on every cycle before the engine ticks.

## Non-Goals

- Persisting full lifecycle metadata (`setup`, `breakeven_moved`, `bars_held`, `initial_stop_px`) to disk for clean-restart fidelity. Future work.
- Auto-resolving quantity drift between the local book and the broker. We log; a human decides.
- Re-submitting protective stops for adopted positions whose brackets are gone. Operator decision.
- Reconciling external order state (open orders without an associated position). Out of scope.

## Policy Decisions

These were settled in brainstorming:

1. **Adoption.** Positions found on Alpaca but absent from the local book are **adopted as monitor-only**: included in risk accounting, but `PositionManager` will not run breakeven, time_stop, or local stop/target detection. Reasoning: we don't know `setup`, `initial_stop_px`, or `bars_held`, so any computed lifecycle action is fiction. The broker's existing equity bracket handles the exit; on the next strategy-driven trade the lifecycle resumes.

2. **Cadence.** Reconcile **at startup and on every cycle, immediately before `engine.tick()`**. Cost: one `GET /v2/positions` per tick (cheap — same loop already calls `get_account()`). Catches the bracket-fired-while-sleeping case that triggered this bug.

3. **Crypto adoption.** Adopt with `stop_px=None`, `target_px=None`. Crypto has no broker-side bracket; an adopted crypto position is naked. Emit a `WARNING`-level `RECONCILE_ADOPTED_CRYPTO_NO_STOP` log on **every cycle** until the position is resolved.

4. **Equity bracket recovery.** When adopting an equity position, fetch its open child orders via a new `list_orders` endpoint and recover `stop_px`, `target_px`, `stop_order_id` from the bracket children. Store them on the adopted `OpenPosition`. The position remains monitor-only — these values are recorded for accurate risk accounting and dashboard display, not for action.

## Architecture

### New module: `state/reconciler.py`

```python
@dataclass
class ReconcileReport:
    closed: list[str]                          # symbols closed by reconciler
    adopted_equity: list[str]
    adopted_crypto: list[str]
    drift: list[tuple[str, float, float]]      # (symbol, book_qty, broker_qty)
    equity_no_bracket: list[str]               # adopted equity missing bracket children


class Reconciler:
    def __init__(
        self,
        alpaca: AlpacaClient,
        ac_configs: dict[str, AssetClassConfig],
        *,
        logger: logging.Logger | None = None,
    ) -> None: ...

    def reconcile(self, book: PositionBook) -> ReconcileReport: ...
```

The class has no internal mutable state; `reconcile` is a pure function of `(book, broker state)` with side effects on `book`. This makes the unit tests trivial (fake `alpaca`, real `book`).

### Wiring in `main.py`

Two integration points:

**Startup (between `book = PositionBook()` and `engine = VWAPWaveEngine(...)`):**
```python
reconciler = Reconciler(alpaca, ac_configs)
report = reconciler.reconcile(book)
logger.info("RECONCILE_STARTUP closed=%d adopted_eq=%d adopted_cr=%d drift=%d no_bracket=%d",
            len(report.closed), len(report.adopted_equity), len(report.adopted_crypto),
            len(report.drift), len(report.equity_no_bracket))
```
If `reconcile` raises, **abort startup** with `sys.exit(1)`. Same pattern as the existing equity-zero check at line 287. Running with unknown position state is unsafe.

**Per-cycle (immediately before `engine.tick(...)` at line 364):**
```python
try:
    report = reconciler.reconcile(book)
    if report.closed or report.adopted_equity or report.adopted_crypto or report.drift:
        logger.info("RECONCILE closed=%d adopted_eq=%d adopted_cr=%d drift=%d",
                    len(report.closed), len(report.adopted_equity),
                    len(report.adopted_crypto), len(report.drift))
except Exception as exc:
    logger.error("RECONCILE_ERROR: %s", exc, exc_info=True)
```
Per-cycle reconcile **never raises out of the cycle** — log and continue; we'll retry next tick.

### `OpenPosition` schema changes (`state/position_book.py`)

Add one new field; relax two:
```python
adopted: bool = False                          # NEW
stop_px: float | None                          # was: float
target_px: float | None                        # was: float
initial_stop_px: float | None = None           # already optional
```

Update `risk_per_share` and `initial_risk_per_share` to return `0.0` when `stop_px` (or `initial_stop_px`) is `None`. `aggregate_open_risk_usd` then yields `0.0` for adopted-without-bracket positions, which is the correct value (we don't know the risk; we don't claim to).

### `PositionManager.on_bar` change (`core/position_manager.py`)

Early guard at the top of `on_bar`:
```python
if pos.adopted:
    pos.bars_held += 1     # informational only; no time_stop branch acts on it
    return []
```
Adopted positions never emit `PositionAction`. The book entry is closed only by the reconciler when Alpaca reports the position gone.

### `OrderExecutor.submit` (`broker/order_executor.py`)

`adopted=False` is the dataclass default — no call-site changes. The executor only constructs trader-opened positions, which are not adopted by definition.

### New `AlpacaClient.list_orders` (`broker/alpaca_client.py`)

```python
def list_orders(
    self,
    *,
    status: str = "open",
    symbols: list[str] | None = None,
    nested: bool = True,
) -> list[dict]:
    """GET /v2/orders — list orders, optionally filtered by status and symbols.

    nested=True returns child legs of bracket orders inside the parent's `legs` field.
    """
    params: dict = {"status": status, "nested": "true" if nested else "false"}
    if symbols:
        params["symbols"] = ",".join(symbols)
    response = self._request("GET", "/v2/orders", params=params)
    return response.json()
```

## Reconciliation Algorithm

```
def reconcile(book) -> ReconcileReport:
    broker_positions = alpaca.get_positions()
    broker_by_symbol = {p["symbol"]: p for p in broker_positions}

    closed, adopted_eq, adopted_cr, drift, no_bracket = [], [], [], [], []

    # 1. Closed: in book, not in broker
    for symbol in list(book.symbols()):
        if symbol not in broker_by_symbol:
            pos = book.close(symbol)
            closed.append(symbol)
            log.info("RECONCILE_CLOSED symbol=%s adopted=%s setup=%s",
                     symbol, pos.adopted if pos else "?", pos.setup if pos else "?")

    # 2. Drift: in both, qty differs beyond epsilon
    QTY_EPS = 1e-6
    for symbol, broker_pos in broker_by_symbol.items():
        local_pos = book.get(symbol)
        if local_pos is None:
            continue
        broker_qty = abs(float(broker_pos["qty"]))
        if abs(local_pos.qty - broker_qty) > QTY_EPS:
            drift.append((symbol, local_pos.qty, broker_qty))
            log.warning("RECONCILE_DRIFT symbol=%s book_qty=%s broker_qty=%s",
                        symbol, local_pos.qty, broker_qty)
            # No mutation — operator alert only.

    # 3. Orphans: in broker, not in book → adopt
    orphan_equity_symbols = []
    orphan_crypto = []
    for symbol, broker_pos in broker_by_symbol.items():
        if book.get(symbol) is not None:
            continue
        ac = _normalize_asset_class(broker_pos.get("asset_class", ""))
        if ac == "equity":
            orphan_equity_symbols.append(symbol)
        elif ac == "crypto":
            orphan_crypto.append(broker_pos)
        else:
            log.warning("RECONCILE_UNKNOWN_ASSET_CLASS symbol=%s class=%s",
                        symbol, broker_pos.get("asset_class"))

    # 3a. Equity adoption: one batched list_orders call to recover bracket children
    bracket_index = {}
    if orphan_equity_symbols:
        open_orders = alpaca.list_orders(status="open", symbols=orphan_equity_symbols, nested=True)
        bracket_index = _index_bracket_children(open_orders)  # {symbol: {"stop": leg, "target": leg}}

    for symbol in orphan_equity_symbols:
        broker_pos = broker_by_symbol[symbol]
        legs = bracket_index.get(symbol, {})
        stop_leg = legs.get("stop")
        target_leg = legs.get("target")
        stop_px = float(stop_leg["stop_price"]) if stop_leg else None
        target_px = float(target_leg["limit_price"]) if target_leg else None
        stop_order_id = stop_leg["id"] if stop_leg else None
        if stop_leg is None and target_leg is None:
            no_bracket.append(symbol)
            log.warning("RECONCILE_EQUITY_NO_BRACKET symbol=%s qty=%s entry=%s",
                        symbol, broker_pos["qty"], broker_pos["avg_entry_price"])
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
        )
        book.add(pos)
        adopted_eq.append(symbol)
        log.info("RECONCILE_ADOPTED_EQUITY symbol=%s side=%s qty=%s entry=%s "
                 "stop=%s target=%s stop_leg=%s",
                 symbol, pos.side, pos.qty, pos.entry_px, pos.stop_px,
                 pos.target_px, pos.stop_order_id)

    # 3b. Crypto adoption: no bracket recovery, naked, loud warning
    for broker_pos in orphan_crypto:
        symbol = broker_pos["symbol"]
        pos = OpenPosition(
            symbol=symbol,
            setup="adopted",
            side=_normalize_side(broker_pos["side"]),
            qty=abs(float(broker_pos["qty"])),
            entry_px=float(broker_pos["avg_entry_price"]),
            stop_px=None,
            target_px=None,
            opened_at=datetime.now(timezone.utc),
            order_id="",
            stop_order_id=None,
            initial_stop_px=None,
            adopted=True,
        )
        book.add(pos)
        adopted_cr.append(symbol)
        log.warning("RECONCILE_ADOPTED_CRYPTO_NO_STOP symbol=%s side=%s qty=%s entry=%s",
                    symbol, pos.side, pos.qty, pos.entry_px)

    # 4. Recurring naked-crypto warning (every cycle, every adopted crypto with no stop).
    #    On the adoption cycle this fires alongside RECONCILE_ADOPTED_CRYPTO_NO_STOP — that
    #    is intentional: two different message names, both useful, no special-casing.
    for pos in book.all():
        if pos.adopted and pos.stop_px is None:
            log.warning("ADOPTED_CRYPTO_NAKED symbol=%s qty=%s entry=%s — manual close required",
                        pos.symbol, pos.qty, pos.entry_px)

    return ReconcileReport(closed, adopted_eq, adopted_cr, drift, no_bracket)
```

**Asset-class normalization.** Per memory observation S20, the canonical name is `"equity"`, while Alpaca's API returns `"us_equity"`. `_normalize_asset_class("us_equity") → "equity"`; `"crypto" → "crypto"`; anything else → unknown.

**Side normalization.** Alpaca returns `"long"` / `"short"` for positions, matching our internal convention. No transformation needed; just pass through with a defensive check.

**Bracket child indexing.** A bracket parent order returned with `nested=true` has `legs: list[dict]`. Children carry the symbol of the parent. Match each leg by type:
- `type in ("stop", "stop_limit")` → stop child
- `type == "limit"` and `side` opposite to entry → target child

Iterate top-level orders; for each whose `legs` is non-empty (parents) or whose `parent_id` is set (children of legs param), index by symbol. Edge case: if the parent has filled and only the children remain alive, they appear as top-level orders with `parent_id` set, not under `legs`. Implementation must handle both shapes — index by `(symbol, type)` across the entire flat list.

## Error Handling

| Failure | Response |
|---------|----------|
| `get_positions()` raises at startup | `sys.exit(1)` with `RECONCILE_STARTUP_FAILED` log |
| `list_orders()` raises at startup | `sys.exit(1)` (we cannot adopt safely without bracket data) |
| `get_positions()` raises mid-cycle | Log `RECONCILE_ERROR`, continue cycle (engine ticks with last known book state) |
| `list_orders()` raises mid-cycle | Log `RECONCILE_ERROR`, skip orphan adoption this tick, retry next |
| Position has unknown `asset_class` | Log `RECONCILE_UNKNOWN_ASSET_CLASS`, skip adoption |
| Adopted equity with no bracket children | Log `RECONCILE_EQUITY_NO_BRACKET`, adopt with `stop_px=None`/`target_px=None`, set `stop_order_id=None` |
| Drift detected | Log `RECONCILE_DRIFT`, **no mutation** |
| `book.add()` raises (symbol already in book) | Should be impossible after closed/drift checks, but defensive: log `RECONCILE_DOUBLE_ADD` and skip |

## Testing

New file `tests/test_reconciler.py`:

1. `test_reconcile_empty_book_empty_broker` — no-op, empty report.
2. `test_reconcile_book_matches_broker_no_changes` — trader-opened positions match broker; no closes, no adoptions, no drift.
3. `test_reconcile_closes_position_when_broker_says_gone` — book has X, broker is empty → `book.close("X")`, `closed=["X"]`.
4. `test_reconcile_adopts_equity_with_alive_bracket` — broker has equity, fake `list_orders` returns parent with legs → adopted with `stop_px`/`target_px`/`stop_order_id` recovered, `adopted=True`.
5. `test_reconcile_adopts_equity_with_orphaned_bracket_children` — children present without parent (parent filled) → still recovered correctly.
6. `test_reconcile_adopts_equity_no_bracket` — broker has equity, no open orders → adopted with all three bracket fields `None`, `equity_no_bracket=[symbol]`.
7. `test_reconcile_adopts_crypto_no_stop` — broker has crypto → adopted with `stop_px=None`, `target_px=None`, warning log.
8. `test_reconcile_logs_drift_no_mutation` — book qty 100, broker qty 50 → `drift=[("X", 100, 50)]`, book unchanged.
9. `test_reconcile_unknown_asset_class_skips_adoption` — log warning, no `book.add`.
10. `test_reconcile_normalizes_us_equity_to_equity` — Alpaca returns `"us_equity"`, treated as equity branch.

New file `tests/test_position_manager_adopted.py` (or extend existing):

11. `test_position_manager_skips_adopted_breakeven` — adopted long, bar at breakeven trigger price → no actions emitted.
12. `test_position_manager_skips_adopted_time_stop` — adopted, `bars_held > max_hold` → no actions, no `book.close`.
13. `test_position_manager_skips_adopted_stop_target` — adopted long, bar.low <= stop_px → no actions.
14. `test_position_manager_increments_bars_held_on_adopted` — bars_held still increments (informational only).

Update `tests/test_position_book.py`:

15. `test_open_position_with_none_stop_yields_zero_risk` — `risk_per_share == 0.0` and `open_risk_usd == 0.0`.

End-to-end smoke (manual): start trader against paper account that has 2 manually-opened positions → expect `RECONCILE_STARTUP adopted_eq=2`, `book.count() == 2` in first `CYCLE_DONE`.

## Files Touched

| File | Change |
|------|--------|
| `state/reconciler.py` | **NEW** — `Reconciler`, `ReconcileReport`, asset-class/side normalizers, bracket-child indexer |
| `state/position_book.py` | Add `adopted: bool = False`; relax `stop_px`/`target_px` to `float \| None`; guard risk-per-share against `None` stop |
| `core/position_manager.py` | Early-return guard for `pos.adopted` |
| `broker/alpaca_client.py` | Add `list_orders` method |
| `main.py` | Instantiate `Reconciler`; call at startup (fail-start on raise); call once per cycle before `engine.tick` (log-and-continue on raise) |
| `tests/test_reconciler.py` | **NEW** — 10 unit tests above |
| `tests/test_position_manager.py` | Add adopted-skip tests |
| `tests/test_position_book.py` | Add `None`-stop test |

## Risk Notes

- **Drift is log-only for both trader-opened and adopted positions.** Strictly, a drift on an adopted position means Alpaca's qty has changed since adoption (e.g., partial manual close), and since adopted positions *are* Alpaca's truth we could update qty silently. We choose not to: a single drift policy across both flavors keeps the reconciler simple and avoids an asymmetry that obscures unexpected state. Operator handles via logs. If this becomes operationally noisy, a future change can auto-resolve adopted-side drift.
- **Adopted positions consume risk slots without being managed.** Risk-of-ruin is bounded because the broker's bracket protects equity adoptions; crypto adoptions are visibly naked in logs.
- **`now()` for `opened_at` on adopted positions** is wrong but harmless: `bars_held` starts at 0 and is never read (adopted positions skip time_stop). Future work: pull `created_at` from Alpaca position metadata if available.
- **No persistence.** Restart still loses `breakeven_moved`, `bars_held`, original `setup` for trader-opened positions. Reconciliation makes them adopted on next start, which de-features them. Acceptable given the current bug; real persistence is a separate spec.
