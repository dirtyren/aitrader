# Broker ↔ MySQL Reconciliation v2

**Date:** 2026-05-28
**Status:** Spec — pending approval
**Supersedes:** `2026-05-22-broker-position-reconciliation-design.md` (the original per-strategy reconciler this design replaces)

## 1. Goal & Invariant

Eliminate the failure mode where Alpaca shows an open position that no
strategy in MySQL is monitoring, without introducing the inverse failure
(closing a real MySQL row because of a transient broker read).

The single invariant defended by this system, at every steady-state moment:

> For every symbol: `Σ (qty of open MySQL rows for that symbol, across all strategies) == broker qty for that symbol`.

When this invariant is violated, exactly one of three causes is true:

1. A real fill happened that MySQL doesn't reflect yet (entry or exit).
   Fix: apply that fill to the right `(strategy, setup)` row using
   `client_order_id` attribution.
2. A transient broker read (empty/partial response, blip).
   Fix: ignore — multi-strike confirmation prevents single-read mutations.
3. An untagged broker position with no MySQL row anywhere (manual UI open,
   retired strategy, pre-migration legacy).
   Fix: alert only — never auto-adopt.

### Non-goals

- Backtest / research mode (live-only).
- Replacing MySQL as source of truth. MySQL stays authoritative for "what
  each strategy thinks it owns"; the reconciler service makes that match
  Alpaca.
- Reconciling broker positions opened before this migration with no
  `client_order_id` — those go through the alert-only path until
  human-resolved.

### Why the previous design fell short

The current `state/reconciler.py` runs once per cycle in *each* strategy
container against shared broker state. Three resulting problems:

- **No per-strategy attribution on close.** When two strategies hold the
  same symbol and one closes, the broker still shows the symbol present.
  The closing strategy's row never gets force-closed (good), but the
  reconciler also has no way to *know* which strategy's row is the
  authoritative one — it logs `RECONCILE_DRIFT_AMBIGUOUS` and gives up.
  The closed strategy's MySQL row sits as a zombie open until manually
  resolved.
- **Single transient broker read can destroy data.** A blip returning
  empty positions causes `close_positions_not_in_broker` to flip every
  open MySQL row to `closed, pnl=0, reason='reconciled_gone'`. The next
  cycle the broker has the positions back, but they've already been
  marked closed, and no strategy adopts them back unless their symbol is
  in `configured_symbols` and not "owned" elsewhere.
- **N concurrent reconcilers race.** Each strategy's reconciler
  independently mutates MySQL based on its own snapshot of broker state.
  Cross-strategy invariants (sum across strategies) are checked from N
  points of view that don't agree.

## 2. Component — `reconciler` Service Container

A new top-level service in `docker-compose.yml`. Single process, single
owner of the cross-strategy invariant. Strategy containers stop mutating
MySQL via reconciliation entirely (they keep mutating MySQL on their own
order submissions and closes — unchanged).

### Service shape

- `command: python -m reconciler.main`
- Loop interval: **30 seconds**, configurable via `RECONCILE_INTERVAL_S`.
- Same MySQL connection env as traders. Same `AlpacaClient` (single
  shared account).
- Restart policy: `unless-stopped`. Depends on `mysql` healthy.
- Owns its own state file `runtime/reconciler_state.json` for
  `last_orders_check_ts` and recovery anchors. Strike counters live in
  MySQL (durable across container rebuilds, queryable from the
  dashboard).

### Per-cycle work (single-threaded, in this order)

1. **Pull broker truth.**
   - `alpaca.get_positions()` → `broker_positions: dict[symbol → broker_qty]`
   - `alpaca.list_orders(status="closed", after=last_orders_check_ts, limit=500, nested=True)` → `recent_fills`
   - On any HTTP error or empty positions list: log, **skip the cycle**;
     do not advance `last_orders_check_ts`. No mutations.

2. **Apply attributed fills to MySQL.** For each filled order:
   - Parse `client_order_id` (see §4).
   - **Untagged** (no prefix or unparseable): write a
     `reconciliation_events` row `type='untagged_fill'`, alert. No
     mutation.
   - **Tagged entry fill, no MySQL row for `(strategy, setup, symbol)`**:
     insert a row (`adopted=False`, `opened_at=fill_time`,
     `entry_px=fill.filled_avg_price`, `qty=fill.filled_qty`,
     `client_order_id=<fill coid>`). Recovery from "submitted, filled,
     crashed before write".
   - **Tagged exit fill, MySQL row open for `(strategy, setup, symbol)`**:
     call `position_closed(symbol, exit_px=fill.filled_avg_price,
     close_reason='broker_fill', setup_name=setup,
     exit_client_order_id=<fill coid>)`. Real exit price, no `pnl=0`
     placeholder.
   - **Tagged but no matching MySQL state** (e.g., already closed by
     this fill on a previous cycle): idempotent noop.

3. **Check the cross-strategy invariant.** For each broker symbol:
   - `mysql_sum = Σ open qty across all strategies (slash/flat normalized)`.
   - If `|mysql_sum − broker_qty| ≤ ε` → reset/clear strike for this
     symbol; continue.
   - Else → enter the multi-strike rule (§3) with direction `qty_drift`.

4. **Detect MySQL-only zombies.** For each `(strategy_id, symbol)` with
   `status=open` in MySQL:
   - If `symbol ∉ broker_positions` AND no tagged exit fill in step 2
     closed it → multi-strike rule, direction `mysql_only`.

5. **Detect broker-only orphans.** For each broker symbol with
   `mysql_sum == 0` AND not explained by a fill in step 2 → multi-strike
   rule, direction `broker_only`.

6. **Heartbeat.** Append `reconciliation_events` row `type='heartbeat'`.
   The dashboard alerts when this row is older than 5 minutes.

### What strategy containers stop doing

Removed from `main.py` and `state/reconciler.py` (the file itself is
deleted at the end of rollout):

- Startup `Reconciler.reconcile(...)`.
- Per-cycle `reconciler.reconcile(...)`.
- `MySQLStore.close_positions_not_in_broker` (kept on the class but
  callable only from the reconciler service).
- `MySQLStore.update_position_qty` and the drift-correction path.

What strategy containers **keep doing** (unchanged):

- Load book from MySQL on startup and rebuild from MySQL each cycle.
- Write to MySQL via `position_opened` / `position_closed` on their own
  orders.
- Manage their own positions (`PositionManager`, exits, breakeven moves).

### Consequences

- The current `Reconciler`'s adoption logic (ATR-based crypto stops,
  equity bracket recovery) goes away for the auto-path. Adoption is now
  an **explicit operator action** (`scripts/reconcile_resolve.py adopt …`,
  see §5).
- `client_order_id` becomes a hard contract enforced at submission time.
  No order leaves the system without a parseable COID.

## 3. Multi-Strike Confirmation Rule

Prevents transient broker reads from destroying MySQL data. Governs when a
reconciler decision becomes a mutation.

### State table — `reconciliation_strikes`

```
id                   BIGINT PK auto
key                  VARCHAR(128)   -- e.g. "qty_drift:BTCUSD"
                                    --      "mysql_only:vwap_wave:AAPL"
                                    --      "broker_only:SOLUSD"
direction            ENUM('qty_drift','mysql_only','broker_only')
strategy_id          INT NULL       -- present for mysql_only/qty_drift
symbol               VARCHAR(32)
strike_count         INT
first_seen_at        DATETIME
last_seen_at         DATETIME
last_observed_state  JSON
resolved             BOOLEAN DEFAULT FALSE
resolved_at          DATETIME NULL
resolved_reason      VARCHAR(64) NULL
```

### Per-cycle algorithm (per anomaly)

1. **Lookup-or-insert** for `key`. If new: `strike_count=1`,
   `first_seen_at=now`.
2. **If row exists, `resolved=TRUE`, anomaly returned**: unresolve →
   `strike_count=1`, `first_seen_at=now`, `resolved=FALSE`. Treat as
   fresh anomaly; do not reuse a stale streak.
3. **If row exists, not resolved**:
   - `now − last_seen_at < min_gap (default 60s)` → noop.
   - Else → `strike_count += 1`, `last_seen_at=now`,
     `last_observed_state=<snapshot>`.
4. **Decision based on `strike_count`**:
   - **strike 1**: log `RECON_ANOMALY_STRIKE_1`, no other action.
   - **strike 2**: log + informational alert via `notifications.py`.
     No mutation.
   - **strike N (default 3)**: act per direction (table below). Mark
     `resolved=TRUE` on success.
5. **Auto-clear.** At cycle start, iterate unresolved strikes. Any
   anomaly no longer present in the current snapshot → `resolved=TRUE`,
   `resolved_reason='self_healed'`, `strike_count=0`. Self-heals leave a
   paper trail.

### Per-direction action at strike N

| Direction | Action at strike N |
|---|---|
| `qty_drift` (mysql_sum ≠ broker_qty AND both > 0) | Alert only. Never auto-correct cross-strategy quantity drift. Disambiguation requires `client_order_id` attribution, which by definition wasn't applied here. Operator-resolved. |
| `mysql_only` (strategy has open MySQL row, broker has no position for symbol AND no tagged fill closed it) | Alert + freeze. Insert `reconciliation_events` row `type='mysql_only_confirmed'`. **Do not auto-close the MySQL row.** Operator decides via `scripts/reconcile_resolve.py`. The MySQL row stays `open` so the strategy continues managing it. |
| `broker_only` (broker position with no MySQL row anywhere AND no tagged fill explains it) | Alert only. **No auto-adoption.** Operator runs `scripts/reconcile_resolve.py adopt --strategy X --setup Y --symbol Z`. |

### Defaults & tunables (env vars on reconciler container)

- `RECONCILE_INTERVAL_S=30`
- `RECONCILE_STRIKE_THRESHOLD=3`
- `RECONCILE_STRIKE_MIN_GAP_S=60`
- `RECONCILE_QTY_EPS=1e-6`
- `SHADOW_MODE=false` (true → log everything, mutate nothing)

### Rationale: why "alert + freeze" instead of auto-close on `mysql_only`

The user's stated constraint: false-negative monitoring (an open broker
position not monitored by any strategy) is unacceptable. False-positive
monitoring (a closed-on-broker MySQL row staying open for a few hours
until human review) is acceptable. The strike rule + freeze policy
inverts the previous code's risk profile to match.

## 4. `client_order_id` Contract & Migration

Mechanism that makes per-`(strategy, setup)` attribution possible. Without
it, §2.2 collapses back to symbol-only logic — the source of the current
bugs.

### Contract

Every order submitted via `OrderExecutor` (entry, exit limit, stop legs,
bracket children) must include a `client_order_id` of the form:

```
aitrader__<strategy>__<setup>__<symbol>__<role>__<short-uuid>
```

- `<strategy>`: `system_name` from config (e.g. `vwap_wave`,
  `rsi_equity`). Sanitized to `[a-z0-9_]`.
- `<setup>`: setup name (e.g. `vwap_bounce`, `rsi_reversion`). Sanitized.
- `<symbol>`: broker-form symbol — flat for crypto (`BTCUSD`, not
  `BTC/USD`). Sanitized.
- `<role>`: one of `entry`, `exit`, `stop`, `target`. Tells the
  reconciler whether a fill opens or closes a position.
- `<short-uuid>`: `uuid4().hex[:8]`. Uniqueness against retries / reuse.

Must remain under Alpaca's 128-char `client_order_id` limit.

### Single chokepoint

New module `broker/client_order_id.py`:

```python
def make_client_order_id(strategy: str, setup: str, symbol: str, role: str) -> str: ...
def parse_client_order_id(coid: str) -> dict | None:  # {strategy, setup, symbol, role} or None
```

`OrderExecutor.__init__` takes `strategy_name: str` and stores it. Every
`submit_order` path inside `OrderExecutor` calls
`make_client_order_id(...)` and passes the result to `AlpacaClient`. A
unit test asserts every `submit_order` invocation in
`broker/order_executor.py` produces a non-empty parseable COID with a
valid role.

`AlpacaClient.submit_order(...)` and bracket-leg helpers gain a
`client_order_id` parameter (already supported by Alpaca's REST API; this
spec only requires threading it through if not already present).

### MySQL changes

Add columns:

- `positions.client_order_id VARCHAR(128) NULL` — entry COID.
- `positions.exit_client_order_id VARCHAR(128) NULL` — exit COID once
  closed.
- `positions.legacy_untagged BOOLEAN DEFAULT FALSE` — set TRUE for
  pre-migration open rows by the schema migration.
- `trades.client_order_id VARCHAR(128) NULL` — entry COID at archive
  time.
- `trades.exit_client_order_id VARCHAR(128) NULL` — exit COID at archive
  time.

These give the reconciler an idempotency key: when applying a fill, it
checks "is this `client_order_id` already on a closed row?" and skips.

### Migration phases

1. **Phase 1 (this spec).** Ship the contract + reconciler service. Every
   *new* order from deploy onward has a tagged COID.
2. **In-flight legacy positions.** Existing MySQL `positions` rows with
   `status='open'` and no COID are flagged `legacy_untagged=TRUE` by the
   schema migration. The reconciler treats these as alert-only:
   - `mysql_only` and `qty_drift` anomalies → alert + freeze, same as
     untagged rows in general.
   - These rows drain naturally as they close through their own setup
     logic over the following trading week.
3. **`broker_only` anomalies discovered post-deploy** → alert only via
   the strike rule. Operator uses `scripts/reconcile_resolve.py` to
   either adopt with explicit attribution or close on Alpaca.
4. **No backfill of `client_order_id` for legacy rows.** They're tracked
   by symbol + the freeze rule until they close.

### Recovery from "submitted, filled, crashed before MySQL write"

Even with tagging, a strategy can: submit order, Alpaca accepts and
fills, container crashes before `position_opened` writes to MySQL. With
this design:

- Reconciler sees the fill on the next 30s cycle.
- COID parses to `(strategy, setup, symbol, entry)`.
- No matching MySQL row → reconciler **inserts** the row in
  `position_opened` shape, with `client_order_id` from the fill.
- The strategy container, on its next cycle, rebuilds its book from
  MySQL and picks up the position. Now monitored.

This is the *only* auto-mutation the reconciler does besides applying
tagged exit fills. It's safe because the COID proves the strategy
intended to open this position and the fill price comes from the actual
broker fill.

## 5. Operator Tools, Tests, Observability, Rollout

### Operator CLI — `scripts/reconcile_resolve.py`

```
reconcile_resolve.py list                     # all unresolved strikes + recent events
reconcile_resolve.py show <strike_id>         # full detail incl. last_observed_state JSON

reconcile_resolve.py close <strike_id> --exit-px <px> --reason <reason>
                                              # mysql_only: close MySQL row with real exit_px
reconcile_resolve.py force-zero <strike_id>   # mysql_only: close as pnl=0, reason='reconciled_gone'
                                              # (the old auto behavior — now requires explicit consent)

reconcile_resolve.py adopt <strike_id> --strategy X --setup Y
                                              # broker_only: insert MySQL row attributed to (X, Y),
                                              # tagged with synthetic COID
                                              # 'aitrader__X__Y__SYM__adopted__<uuid>'

reconcile_resolve.py extend <strike_id>       # reset strike_count to 0, reopen the case
reconcile_resolve.py dismiss <strike_id>      # mark resolved without action
```

Every action writes a `reconciliation_events` audit row with operator
note (default: system user) and the resolved strike's snapshot.

### Notifications

Reuses `notifications.py`. New helpers:

- `send_reconcile_alert(direction, symbol, strategy, snapshot, strike_count)`
  — informational at strike 2; urgent at strike N.
- `send_reconcile_heartbeat_stale(last_seen_at)` — fired by the dashboard
  / external watchdog when `reconciliation_events` heartbeat is older
  than 5 minutes.

### Dashboard surface

A new tab `Reconciliation`:

- Top: heartbeat freshness — green ≤ 60s, yellow ≤ 5min, red beyond.
- Middle: open/unresolved strikes — direction, symbol, strategy,
  strike_count, first_seen, last_seen, last_observed_state.
- Bottom: last 50 reconciliation events.

Read-only. All resolutions go through the CLI for auditability.

### Tests (TDD, before any new code ships)

1. **`broker/client_order_id.py`** — round-trip parse, sanitization edge
   cases, length cap, role enum validation.
2. **`OrderExecutor`** — every submit path produces a tagged COID with
   the right role; assert with a fake `AlpacaClient` capturing
   `client_order_id` per call.
3. **`MySQLStore`** — schema migration adds columns + tables
   idempotently; existing-DB upgrade test (run `ensure_schema()` twice).
4. **`reconciler/main.py`** — unit tests with a fake `AlpacaClient`:
   - tagged entry fill, no MySQL row → row inserted with broker fill
     price.
   - tagged exit fill, MySQL row open → row closed with broker fill
     price + reason `broker_fill`.
   - tagged exit fill, MySQL row already closed → idempotent noop.
   - untagged broker position (`broker_only`) — strike progression
     1→2→3, no mutation.
   - untagged exit fill (parse fails) → `untagged_fill` event, no
     mutation.
   - `mysql_only` direction across 3 strikes, broker recovers on strike
     2 → strike auto-cleared, no mutation.
   - `mysql_only` direction across 3 strikes, sustained → alert + freeze,
     MySQL row stays open.
   - cross-strategy: A holds 1, B holds 1, broker shows 2 → invariant
     satisfied, no anomaly.
   - cross-strategy: A holds 1, B holds 1, broker shows 1 → `qty_drift`
     strikes, alert only at strike N, no mutation.
   - transient empty broker positions list → cycle skipped,
     `last_orders_check_ts` not advanced, no mutations.
5. **End-to-end docker-compose smoke** — bring up the stack; simulate
   (a) crash-before-write recovery, (b) untagged broker position alert
   flow.

### Rollout sequence (calm trading window — weekend)

1. Deploy MySQL schema migration (new columns + tables). Idempotent;
   safe with traders running old code.
2. Deploy strategy containers with COID tagging enabled and the *old*
   `state/reconciler.py` still active. Verify `positions.client_order_id`
   populates for new orders for one trading session.
3. Deploy the new `reconciler` service in **shadow mode**
   (`SHADOW_MODE=true`): writes `reconciliation_events` and strikes,
   never mutates positions. Run for one trading session, audit events.
4. Disable `state/reconciler.py` calls in `main.py` (single PR removing
   those call sites; keep the file in tree for one sprint).
5. Switch `reconciler` service out of shadow mode.
6. After one trading week with no incidents, delete `state/reconciler.py`
   and its tests.

### Code organization

```
reconciler/
  __init__.py
  main.py            # entry point, the loop
  fills.py           # apply_tagged_fill(...) — idempotent
  invariant.py       # check_cross_strategy_invariant(...)
  strikes.py         # strike_table CRUD + decision
  events.py          # reconciliation_events writer
broker/
  client_order_id.py # make/parse helpers
  order_executor.py  # +strategy_name, COID on every submit
  alpaca_client.py   # +client_order_id parameter on submit paths
state/
  mysql_store.py     # +columns +tables +new methods (no logic moved out yet)
  reconciler.py      # call sites removed; file deleted at rollout step 6
scripts/
  reconcile_resolve.py
ui/
  dashboard.py       # +Reconciliation tab
docker-compose.yml   # +reconciler service
main.py              # remove startup/cycle reconciler calls
```

## 6. Summary

1. Single invariant: `Σ open MySQL qty per symbol == broker qty per symbol`.
2. `client_order_id` contract tags every order with
   `(strategy, setup, symbol, role)` so fills are attributable.
3. Dedicated `reconciler` container, 30s loop, owns the invariant.
   Strategy containers stop reconciling.
4. Multi-strike rule (default 3 strikes, ≥60s apart) prevents transient
   broker reads from mutating MySQL.
5. Auto-mutations are limited to: applying tagged exit fills, and
   inserting MySQL rows for tagged entry fills with no row
   (crash-before-write recovery). Both safe — COID proves attribution.
6. All other anomalies (`qty_drift`, `mysql_only`, `broker_only`) → alert
   + freeze. Operator resolves via `scripts/reconcile_resolve.py`. The
   MySQL row never silently flips to `closed pnl=0`.
7. Phased rollout with shadow mode before any mutation switches on.
