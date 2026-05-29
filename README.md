# aitrader

Autonomous multi-strategy intraday trading platform for **equities, ETFs, and crypto** via Alpaca Markets. Each strategy runs in its own container against a shared MySQL store, with a dedicated reconciler service that owns the broker↔MySQL invariant, a Streamlit dashboard for visibility, and an operator CLI for resolving anomalies.

---

## What aitrader does

- **Trades autonomously** — bar-close scheduler wakes every N minutes, fetches fresh bars, runs the active strategy's setup state machine, evaluates risk filters, sizes orders against ATR, and submits via Alpaca. Live execution and backtesting share the same `SessionContext`, setup, filter, and risk classes — only the bar source and order sink differ.

- **Runs many strategies side-by-side** — each strategy lives in its own docker-compose service with its own `config/settings_<strategy>.yaml`, its own log file, and its own slice of the `positions` / `trades` tables (keyed by `strategy_id`). Strategies do not share in-memory state; coordination happens through MySQL.

- **Persists every trade in MySQL** — `state/mysql_store.py` is the single writer. Positions, trades, daily stats, and reconciliation events all live in one schema (`state/schema.sql`). The dashboard reads from the same schema; nothing on disk holds source-of-truth state.

- **Reconciles broker state continuously** — a dedicated `reconciler` service runs every 30 seconds, applies tagged Alpaca fills to the right MySQL row (using `client_order_id` attribution), checks the cross-strategy invariant `Σ open MySQL qty per symbol == broker qty per symbol`, and emits multi-strike-confirmed alerts when state diverges. Strategies never reconcile broker state themselves.

- **Defends two correctness invariants:**
  1. **Every order is attributable.** `OrderExecutor` mints a structured `client_order_id` (`aitrader__strategy__setup__symbol__role__uuid8`) on every submit. Fills are matched back to their MySQL row by parsing this COID — even across strategy crashes, container restarts, and crypto symbol-format mismatches.
  2. **Transient broker reads cannot destroy MySQL data.** Every reconciliation anomaly must be confirmed on N consecutive cycles (default 3) ≥ 60s apart before any action is taken; anomalies that disappear self-heal silently.

- **Walk-forward optimizes** — `scripts/run_wfo.py` tunes per-`(symbol, timeframe)` parameters over rolling IS/OOS windows, gates results behind a Pardo-style fitness threshold (`WFE ≥ 0.5` AND total OOS PnL > 0), and produces `live_overrides.yaml` candidates the dashboard's WFO tab can approve into the live trader's parameter layer.

- **Surfaces operational state** — Streamlit dashboard (behind nginx + basic auth) shows: per-strategy stats, live open positions across all strategies, configuration audit, log tail, reconciliation strikes + heartbeat freshness, and WFO candidates.

- **Halts on emergency drawdowns** — three-tier circuit breaker. L1 cuts size in half; L2 blocks new entries for 24h; L3 writes `lock.file` and exits. The lock-file guard runs before any heavy import, so an unresolved emergency cannot be bypassed.

---

## Strategies

Each strategy is enabled in its own settings file and runs in its own container. The `setups:` block of any settings file flips individual setup state machines on/off.

| Setup | File | Trade idea |
|---|---|---|
| `price_discovery` | `setup_price_discovery.py` | Band breakout + acceptance + retest entry |
| `fade_extreme` | `setup_fade_extreme.py` | Scale-in fades against value-area extremes on balance days |
| `return_to_value` | `setup_return_to_value.py` | Failed discovery move re-entering the value area |
| `vwap_bounce` | `setup_vwap_bounce.py` | Trend-day reclaim after a sub-VWAP liquidity trap |
| `vwap_dev_bands` | `setup_vwap_dev_bands.py` | Mean-reversion fades at ±Nσ VWAP bands |
| `rsi_reversion` | `setup_rsi.py` | RSI-based reversion with fixed stop-loss percentage |
| `initial_balance` | `setup_initial_balance.py` | Initial-balance break + retest |
| `orb_vwap` | `setup_orb_vwap.py` | Opening-range breakout filtered by VWAP |

Strategies are composed at boot from the `setups:` block in `config/settings_*.yaml`; one container can run several setups against the same symbol universe.

---

## Architecture

```
aitrader/
├── config/
│   ├── settings.yaml                 # Default (vwap_wave) strategy
│   ├── settings_rsi_*.yaml           # Per-strategy configs (one per container)
│   ├── settings_vwap_bands_*.yaml
│   ├── settings_orb_*.yaml
│   ├── settings_ib*.yaml
│   ├── wfo.yaml                      # Walk-forward optimizer config
│   └── .env.example                  # ALPACA_*, TELEGRAM_*, DASH_*, MYSQL_*
├── core/                             # Domain primitives shared by live + backtest
│   ├── bar.py / vwap.py / atr.py / acceptance.py
│   ├── asset_class.py session.py position_manager.py
├── strategies/                       # 8 setup state machines + regime detector
├── risk/
│   ├── circuit_breakers.py           # L1/L2/L3 tiered drawdown halts
│   ├── filters.py                    # 8 entry filters + pipeline
│   ├── sizing.py                     # ATR-based sizing
│   └── manager.py                    # pipeline → sizing → RiskDecision
├── state/                            # MySQL = source of truth for positions/trades
│   ├── schema.sql                    # Tables: strategies, positions, trades,
│   │                                 #   daily_stats, reconciliation_strikes,
│   │                                 #   reconciliation_events
│   ├── mysql_store.py                # Single writer; ORM + idempotent migrations
│   ├── position_book.py              # Per-strategy in-memory cache
│   ├── daily_ledger.py               # Per-day P&L + consecutive losses
│   └── dashboard_state.py            # Atomic JSON snapshots (legacy)
├── broker/
│   ├── alpaca_client.py              # REST API (orders, account, bars, brackets)
│   ├── alpaca_data.py                # Bars wrapper + parquet cache
│   ├── client_order_id.py            # COID format helpers (Plan 2)
│   ├── order_executor.py             # SetupSignal + RiskDecision → tagged order
│   └── symbol.py                     # Equity vs crypto symbol helpers
├── reconciler/                       # Dedicated reconciler service (Plan 3)
│   ├── main.py                       # Loop: pull → apply fills → invariant → strikes → heartbeat
│   ├── config.py                     # Env-driven ReconcilerConfig
│   ├── state.py                      # Persistent last_orders_check_ts (atomic JSON)
│   ├── fills.py                      # apply_tagged_fill (entry recovery + exit close)
│   ├── invariant.py                  # qty_drift / mysql_only / broker_only detection
│   ├── strikes.py                    # Multi-strike confirmation + auto-clear self-heal
│   └── events.py                     # reconciliation_events writer
├── scheduler/
│   ├── bar_clock.py                  # next_boundary, sleep_until
│   └── loop.py                       # VWAPWaveEngine.tick (bar-close cycle)
├── backtest/
│   ├── intraday_replay.py            # Shared-engine historical replay
│   ├── fill_engine.py                # SimulatedFillEngine
│   └── performance.py                # compute_metrics (R-multiple model)
├── ui/
│   ├── dashboard.py                  # Streamlit entry point — 6 tabs
│   ├── tabs/
│   │   ├── strategies_tab.py         # Per-strategy stats
│   │   ├── live_tab.py               # Open positions across strategies
│   │   ├── reconciliation_tab.py     # Heartbeat + strikes + events
│   │   ├── config_tab.py             # Read-only configuration audit
│   │   └── logs_panel.py             # Log tail per strategy
│   └── data/                         # Read-only repos: positions, trades,
│                                     #   reconciliation, stats, configs
├── scripts/
│   ├── run_backtest.py               # Standalone backtest runner
│   ├── run_wfo.py                    # Walk-forward optimizer
│   ├── reconcile_resolve.py          # Operator CLI (Plan 4)
│   ├── strategy_stats.py             # Quick MySQL stats CLI
│   ├── check_trades.py               # Unreflected-trades audit
│   └── *_sweep.py                    # Per-setup parameter sweeps
├── nginx/                            # Reverse proxy + basic auth + TLS
├── notifications.py                  # Telegram alerts (positions + reconciliation)
├── docker-compose.yml                # mysql + 9 trader containers + reconciler + dashboard + nginx
├── Dockerfile                        # Shared image for traders / reconciler / dashboard
└── tests/                            # 66 test files; ~430 tests, ~10s in docker
```

---

## Setup

### Requirements

- Docker + Docker Compose
- Alpaca Markets account (paper or live)
- (Optional) Telegram bot for trade notifications

### Configure environment

```bash
cp config/.env.example config/.env
```

Edit `config/.env`:

```
# Required
ALPACA_API_KEY=...
ALPACA_SECRET_KEY=...
ALPACA_BASE_URL=https://paper-api.alpaca.markets

# Dashboard auth (basic auth via nginx)
DASH_USER=admin
DASH_PASSWORD=<choose-something>

# Optional — Telegram for position-open + reconciliation alerts
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=

# Operational tuning
TRADING_ENV=production               # 'test' bypasses lock.file guard
SHADOW_MODE=true                     # Reconciler audits without mutating
RECONCILE_INTERVAL_S=30
RECONCILE_STRIKE_THRESHOLD=3
RECONCILE_STRIKE_MIN_GAP_S=60
RECONCILE_HEARTBEAT_STALE_AFTER_S=300
```

`MYSQL_*` variables are set by `docker-compose.yml` and don't need to be in `.env`.

### Bring up the stack

```bash
docker compose up -d mysql                # initializes schema from state/schema.sql
docker compose up -d trader               # default vwap_wave strategy
docker compose up -d trader-rsi-equity    # any of the 9 strategy containers
docker compose up -d reconciler           # 30s reconciliation loop (shadow mode by default)
docker compose up -d dashboard nginx      # https://localhost (basic auth)
```

Or bring everything up at once:

```bash
docker compose up -d
```

The compose file defines 9 trader containers (`trader`, `trader-rsi-equity`, `trader-rsi-crypto`, `trader-ib`, `trader-ib-crypto`, `trader-vwap-bands-equity`, `trader-vwap-bands-crypto`, `trader-orb-equity`, `trader-orb-crypto`) — each runs `python main.py --config config/settings_<strategy>.yaml` against the shared MySQL.

---

## Operating

### Dashboard

Browse to `https://<host>` (basic auth, served by nginx). Six tabs:

- **Strategies** — per-strategy aggregate stats over a configurable lookback window.
- **Live Trading** — open positions across all strategies, current P&L, age. Auto-refreshes every 5s.
- **Reconciliation** — reconciler heartbeat freshness banner (green ≤60s / orange ≤5min / red beyond), unresolved strikes table, recent events feed with type filter, and per-direction CLI snippets in a "How to resolve" expander.
- **Configuration** — read-only audit of every loaded `settings_*.yaml`.
- **Logs** — tail of any strategy's log file (or the dashboard's own log).
- **WFO** — review and approve walk-forward optimizer candidates per symbol.

### Reconciler

Default behavior — `SHADOW_MODE=true` — writes `shadow_would_apply_fill` events to the audit table without mutating positions. Use this for one trading session after deployment, observe the `Reconciliation` tab for any anomalies, then flip to `false`:

```bash
SHADOW_MODE=false docker compose up -d reconciler
```

### Operator CLI — resolving reconciliation strikes

When the reconciler confirms an anomaly across 3 cycles, it freezes (alerts only, never mutates). The operator resolves via `scripts/reconcile_resolve.py`:

```bash
docker compose exec trader python scripts/reconcile_resolve.py list
docker compose exec trader python scripts/reconcile_resolve.py show <id>

# mysql_only strike (open in MySQL but gone from broker):
docker compose exec trader python scripts/reconcile_resolve.py close <id> \
    --exit-px <price> --setup <name> --note "<why>"
# Or close as pnl=0 (only when you've confirmed it's a phantom row):
docker compose exec trader python scripts/reconcile_resolve.py force-zero <id> \
    --setup <name> --note "<why>"

# broker_only strike (broker has position with no MySQL row):
docker compose exec trader python scripts/reconcile_resolve.py adopt <id> \
    --strategy <name> --setup <name> --side <long|short> \
    --qty <q> --entry-px <p> --asset-class <equity|crypto> --note "<why>"

# qty_drift or anything where you want more cycles before acting:
docker compose exec trader python scripts/reconcile_resolve.py extend <id> --note "<why>"

# Known external trade or already-handled-out-of-band:
docker compose exec trader python scripts/reconcile_resolve.py dismiss <id> --note "<why>"
```

Every action writes a `reconciliation_events` row of `type='operator_action'` for audit trail. The dashboard displays the events feed; nothing happens silently.

### Live trading vs paper

Live trading is gated on the `system.trading_env` field in each strategy's settings file (`paper` or `live`). Flipping to `live` is intentional and ungated — set `ALPACA_BASE_URL` to `https://api.alpaca.markets` accordingly.

---

## Backtest

```bash
docker compose run --rm trader python scripts/run_backtest.py --config config/settings.yaml
```

Or programmatically:

```python
from backtest.intraday_replay import IntradayReplay
import yaml, main as boot

cfg = yaml.safe_load(open("config/settings.yaml"))
ac_configs = boot.build_asset_class_configs(cfg)
symbols = [(s, ac) for ac, raw in cfg["asset_classes"].items() for s in raw["symbols"]]

result = IntradayReplay(symbols=symbols, asset_class_configs=ac_configs,
                        bars=..., initial_equity=cfg["backtest"]["initial_equity"],
                        config=cfg).run()
print(result.metrics)
```

The same `core/` and `strategies/` classes that drive live trading drive the backtest — no parallel implementation.

---

## Walk-Forward Optimization

```bash
docker compose run --rm trader python scripts/run_wfo.py --config config/wfo.yaml
```

Tunes setup parameters per `(symbol, timeframe)` over rolling IS/OOS windows across the broker's tradable universe. Output:

- `runtime/wfo/<run_id>/results.parquet` — every `(symbol, timeframe, walk, combo)` row.
- `runtime/wfo/<run_id>/live_overrides.yaml` — per-symbol best `(timeframe, setup, params)` for symbols whose aggregate **WFE ≥ 0.5** AND total OOS P&L > 0 (Pardo gate).
- `runtime/wfo/<run_id>/summary.md` — ranked human-readable table.

Approval flow: the dashboard's **WFO** tab promotes per-symbol candidates from a run into `runtime/wfo/active/live_overrides.yaml`, which `main.py` reads at boot and layers on top of `config/settings.yaml`. Approvals take effect on the next trader restart; `runtime/wfo/active/audit.jsonl` records who approved what.

Tunables: `config/wfo.yaml` (universe scan, IS/OOS lengths, parameter grids per setup, fitness floor, gate thresholds).

---

## Reconciliation v2 design

The reconciliation system is documented in detail across four spec/plan pairs:

- `docs/superpowers/specs/2026-05-28-broker-mysql-reconciliation-design.md` — full design.
- `docs/superpowers/plans/2026-05-28-broker-mysql-reconciliation-p1-schema.md` — schema additions.
- `docs/superpowers/plans/2026-05-28-broker-mysql-reconciliation-p2-client-order-id.md` — COID contract.
- `docs/superpowers/plans/2026-05-28-broker-mysql-reconciliation-p3-reconciler-service.md` — dedicated service.
- `docs/superpowers/plans/2026-05-28-broker-mysql-reconciliation-p4-operator-cli-dashboard.md` — operator UX.

Three guarantees the system makes:

1. **Every order is COID-tagged.** `OrderExecutor` requires a `strategy_name` at construction and mints `aitrader__strategy__setup__symbol__role__uuid8` for every submit (entry, exit, target, stop). Empty-string COIDs are suppressed before reaching Alpaca.
2. **Every fill is attributable.** The reconciler pulls fills with `list_orders(after=last_check_ts)`, parses the COID, and applies the right MySQL mutation: insert (entry recovery for "submitted, filled, crashed before MySQL write"), close (matched against open `(strategy, setup, symbol)` row), or noop (idempotent).
3. **No silent data loss.** Cross-strategy invariant `Σ open MySQL qty per symbol == broker qty per symbol` is checked every cycle. Violations enter the strike rule (3 strikes ≥60s apart) before any alert fires; anomalies that self-heal leave only an audit trail. Auto-mutation is limited to the two unambiguous cases above; everything else is operator-resolved via the CLI.

---

## Circuit breakers / lock-file recovery

Three escalating tiers (tunable in each strategy's `risk.circuit_breaker` block):

- **L1** (default −1.5% intraday) — position sizes halved.
- **L2** (default −2.5% intraday) — new entries blocked for 24h.
- **L3** (default −5% peak-to-valley) — `lock.file` written, `sys.exit(1)`.

Recovery: review logs (Logs tab, or `logs/<strategy>.log`), perform post-mortem, then `rm lock.file` and restart. The lock-file guard runs before any heavy import, so an unresolved emergency halt cannot be bypassed by a subsequent code path.

---

## Testing

The full suite runs in docker against an in-memory SQLite engine for the MySQL paths:

```bash
docker compose run --rm -e TRADING_ENV=test trader pytest --ignore=tests/test_main_overrides.py
```

Approximately **430 tests, ~10 seconds**. No broker connectivity required; mocks are used throughout.

Smoke-import the boot path:

```bash
docker compose run --rm -e TRADING_ENV=test \
    -e ALPACA_API_KEY=x -e ALPACA_SECRET_KEY=x \
    trader python -c "import main; print('ok')"
```

---

## Notifications

Set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` in `config/.env` to receive:

- **Position open alerts** — every entry, with side / qty / entry / stop / target / R-multiple estimate.
- **Reconciliation alerts** — at strike 2 (informational), at strike 3 (frozen — needs operator action).
- **Heartbeat staleness alert** — fires when no reconciler heartbeat for ≥ 5 minutes (configurable).

Without Telegram credentials configured, all notifications silently no-op (debug-level log only).
