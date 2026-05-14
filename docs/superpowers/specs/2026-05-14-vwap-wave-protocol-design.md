# VWAP Wave Protocol — Design Spec

- **Date:** 2026-05-14
- **Status:** Draft, pending implementation
- **Branch:** `feature/vwap-wave-protocol` (recommended)
- **Replaces:** the HMM-based regime trader currently on `main`

## 1. Goals & Scope

Replace the existing HMM regime trader entirely with an intraday execution engine implementing the **VWAP Wave Protocol**: institutional-style mean-reversion and price-discovery setups around session-anchored VWAP and its ±1σ value-area bands.

**In scope (v1):**
- Full deletion of the HMM stack (`engine/`, `core/feature_eng.py`, `core/orchestrator.py`, `strategies/vol_allocation.py`, `backtest/walk_forward.py`).
- Multi-asset Alpaca trading: equities, ETFs, crypto (single broker).
- All four protocol setups: Price Discovery Continuation, Fade Value Extremes, Return to Value, VWAP Bounce.
- Bar-close scheduler on 5-min bars (configurable).
- Backtest engine sharing code with live engine.
- Paper trading wiring; live execution **gated** behind explicit config flag.

**Out of scope (v1):**
- Live trading enabled by default (gated for v1.1).
- Websocket streaming (REST + bar-close scheduler only).
- Economic-calendar API integration (config-driven blackouts only).
- Futures, options, OTC, or non-Alpaca brokers.
- Multi-account or sub-account support.

## 2. Strategy Summary

VWAP is treated as a **dynamic Point of Control**, not a static support/resistance line. The market exists in two states — *transacting in fair value* (within the ±1σ value area) or *searching for new value* (outside it). The four setups are:

1. **Price Discovery Continuation** — breakout + acceptance + backtest entry, target prior value zone.
2. **Fade Value Area Extremes** — on balance days, scale into rejections at the bands, target VWAP.
3. **Return to Value** — failed discovery move re-enters value area, target VWAP.
4. **VWAP Bounce** — on trend days only, trade the reclaim of VWAP after a sub-VWAP liquidity trap.

Capital preservation rules ("stay flat" criteria): opening 15-minute blackout, scheduled news windows, volume-deficit detection, two-consecutive-loss flat rule (per symbol).

## 3. Execution Loop (Bar-Close Scheduler)

The engine runs a single async-style loop:

```
sleep_until_next_5m_boundary(now, asset_class)   # +5s grace
for symbol in watchlist:
    bars = alpaca_data.get_bars(symbol, "5Min", since=session_start)
    ctx  = SessionContext.update(symbol, bars)    # VWAP, σ-bands, regime
    for setup in setups[symbol]:
        signal = setup.check(ctx)
        if signal and filters.passes(signal, ctx, ledger):
            order = sizing.size_position(signal, ctx, equity)
            order_executor.submit(order, ctx)
manage_open_positions()       # stops/targets/breakeven/time-stop
write_dashboard_state()
```

**Why bar-close, not streaming or polling.** The protocol's acceptance rule ("multiple consecutive candle closes outside the band") is fundamentally bar-close-driven. Polling wastes REST calls; streaming requires websocket plumbing not justified for a 5-min strategy. Falls back to polling (60s) if a wake is missed.

## 4. Module Map

### Deleted
- `engine/hmm_model.py`, `engine/regime_classifier.py`, `engine/__init__.py`
- `core/feature_eng.py`, `core/orchestrator.py`
- `strategies/vol_allocation.py`
- `backtest/walk_forward.py`

### Kept (unchanged or near-unchanged)
- `risk/circuit_breakers.py` — tier logic intact; thresholds retuned for intraday.
- `broker/alpaca_client.py` — extended, not replaced.
- `broker/order_executor.py` — light edits for bracket/virtual stops.
- `ui/logging_setup.py`
- Lock-file safety pattern in `main.py` and `runtime/trading_state.json`.

### Modified
- `risk/manager.py` — becomes thin façade over `filters.py` + `sizing.py`.
- `core/portfolio.py` → split into `state/position_book.py` and `state/daily_ledger.py`.
- `ui/dashboard.py` — replace regime panels with VWAP chart, open setups, filter audit, daily P&L per symbol/setup.
- `main.py` — boots the bar-close scheduler.
- `config/settings.yaml` — full new schema (Section 9).

### New
```
core/
  session.py          SessionContext per symbol (state, reset rules)
  vwap.py             Incremental VWAP + ±σ bands
  acceptance.py       N consecutive closes + min-distance ATR
strategies/
  base_setup.py       Abstract Setup, SetupSignal
  setup_price_discovery.py
  setup_fade_extreme.py
  setup_return_to_value.py
  setup_vwap_bounce.py
  regime_detector.py  Range/Discovery/Trend/Undefined classifier
scheduler/
  bar_clock.py        sleep_until_next_boundary()
  loop.py             Main scheduler loop
broker/
  alpaca_data.py      Bars wrapper (stocks + crypto), retry, symbol normalization
risk/
  filters.py          Composable EntryFilter pipeline
  sizing.py           ATR-based position sizing + caps
state/
  position_book.py    Open positions ledger (in-memory + JSON-persisted)
  daily_ledger.py     Per-day P&L, win/loss streak, equity, day boundary
backtest/
  intraday_replay.py  Bar replayer using shared engine
  fill_engine.py      SimulatedFillEngine
```

### Asset-class abstraction

All asset-class branching is concentrated in `SessionContext`'s `AssetClassConfig`:

| Field | Equity | Crypto |
|---|---|---|
| Session reset | 09:30 ET | 00:00 UTC |
| Trading window | 09:30–16:00 ET | 24/7 |
| Opening blackout | 15 min | 15 min |
| Bar timeframe | 5Min | 5Min |
| Symbol format | `AAPL` | `BTC/USD` |
| Slippage (bps) | 2 | 5 |
| Stop type | broker-side native | engine-managed virtual |

## 5. Setup State Machines

Shared primitives (consumed by all setups):

- `value_area = [vwap − σ, vwap + σ]`
- `acceptance(bars, level, side, n=2, min_distance_atr=0.25)` — n consecutive closes on `side`, furthest close ≥ `min_distance_atr × ATR(14)` away.
- `is_trend_day(ctx)` — day_range ≥ 1.5 × avg_20d_range AND <30% of bars closed inside value area.
- `is_balance_day(ctx)` — ≥60% of bars closed inside value area, day_range ≤ 1.0 × avg.
- `regime ∈ {Range, Discovery, Trend, Undefined}` — refreshed each bar by `regime_detector`.

### Setup 1 — Price Discovery Continuation

| State | Transition |
|---|---|
| IDLE → BREAKOUT_PENDING | Bar closes outside ±σ band |
| BREAKOUT_PENDING → ACCEPTED | `acceptance(n=2, min_distance_atr=0.25)` outside |
| BREAKOUT_PENDING → IDLE | Bar closes back inside value area |
| ACCEPTED → ARMED | Price retraces to within 0.1 × ATR of breached band |
| ARMED → FILLED | Wick into band + close in breakout direction → submit limit at band |
| ARMED → EXPIRED | Bar closes back inside value area, OR 6 bars elapse |

- **Stop:** beyond test candle's extreme (≈ 1 × ATR).
- **Target:** prior session's value-area high/low, or +1.5R, whichever closer.
- **Direction:** long if breakout above +σ, short if below −σ.

### Setup 2 — Fade Value Area Extremes (Balance Days)

| State | Transition |
|---|---|
| IDLE → ELIGIBLE | `is_balance_day(ctx)` AND ≥6 consecutive bars (30 min) inside value area |
| ELIGIBLE → REJECTION_DETECTED | Bar wicks above +σ (or below −σ) AND closes back inside |
| REJECTION_DETECTED → SCALING | Submit first scale-in (40% size) at current price |
| SCALING → FULL | Add 2nd (35%) at +0.25 × ATR beyond, 3rd (25%) at +0.50 × ATR beyond |
| SCALING/FULL → STOPPED | Aggregate stop hit, OR regime flips to Discovery |

- **Stop:** band ± 0.5 × ATR beyond rejection bar's extreme.
- **Target:** VWAP primary; opposite band on continuation.
- **Sizing:** total risk capped at `max_risk_per_trade`, split per `scale_weights = [0.4, 0.35, 0.25]`.

### Setup 3 — Return to Value (mirror of Setup 1)

| State | Transition |
|---|---|
| IDLE → REJECTION | Was outside value area; closes back inside |
| REJECTION → REENTRY_ACCEPTED | `acceptance(n=2)` inside |
| REENTRY_ACCEPTED → ARMED | Retests band from inside (within 0.1 × ATR) |
| ARMED → FILLED | Limit at band fills → trade toward VWAP |
| ARMED → EXPIRED | Re-breaks band outward, OR 6 bars elapse |

- **Stop:** beyond band by 0.5 × ATR.
- **Target:** VWAP.
- **Direction:** opposite of prior discovery direction.

### Setup 4 — VWAP Bounce (Trend Days only)

| State | Transition |
|---|---|
| IDLE → TREND_CONFIRMED | `is_trend_day(ctx)` AND ≥70% of bars on one side of VWAP |
| TREND_CONFIRMED → SUB_VWAP_TRAP | Price dips through VWAP against trend |
| SUB_VWAP_TRAP → RECLAIM | Bar closes back across VWAP in trend direction |
| RECLAIM → ARMED | Wait for first pullback to VWAP after reclaim |
| ARMED → FILLED | Limit at VWAP fills; stop below reclaim bar's low (or above for shorts) |
| ARMED → EXPIRED | Closes through VWAP again against trend, OR 4 bars w/o pullback |

- **Stop:** beyond the trap bar (≈ 1.25 × ATR).
- **Target:** day's high/low, or +2R.

### Cross-setup coordination
- One active setup per symbol at a time (whichever armed first wins; others self-skip until it resolves).
- The regime classifier (`regime_detector.py`) is **informational, not a gate** — each setup encodes its own preconditions in its state machine (Setup 2 explicitly checks `is_balance_day(ctx)`; Setup 4 checks `is_trend_day(ctx)`; Setups 1 & 3 are driven by band-cross transitions). The regime label is logged and surfaced in the dashboard but does not veto setups directly.
- Setup quality / decay (Principle 5): each setup tracks `level_touch_count` for the level it's working off; refuses to arm if `touch_count > 1` on the same session level.

## 6. Risk, Filters & Sizing

### Filter pipeline (sequential; first reject wins)

```
1. SystemHaltedFilter         circuit_breaker level >= 2 OR lock.file present
2. SessionWindowFilter        session open AND past opening blackout
3. NewsBlackoutFilter         now ± 5 min ∈ any scheduled window
4. VolumeDeficitFilter        last 6-bar volume < 70% of trailing-20-day same-time-of-day avg
5. ConsecutiveLossFilter      ≥ 2 consecutive losses today (per symbol; configurable)
6. ConcurrentPositionFilter   max_concurrent_positions reached
7. SetupCooldownFilter        same setup fired on same symbol < cooldown_bars ago
8. RiskBudgetFilter           aggregate open risk would exceed daily_risk_cap
```

Each filter is a pure class with `check(signal, ctx, ledger) -> FilterResult(passed: bool, reason: str)`. Results are logged for the dashboard's filter audit panel.

### Position sizing

```
risk_dollars = equity × max_risk_per_trade        # default 0.005
stop_distance = atr_mult × ATR(14, bar_size)      # per-setup multiplier
shares = floor(risk_dollars / stop_distance)
notional = shares × entry_price
notional = min(notional, max_notional_per_trade)  # hard cap
shares = floor(notional / entry_price)            # recompute after cap
```

Caps:
- `max_notional_per_trade` (config: `risk.max_notional_per_trade`, default `equity × 0.20`).
- `max_concurrent_positions` (system-wide, see filter pipeline).

Per-setup ATR multipliers:

| Setup | atr_mult |
|---|---|
| 1 — Price Discovery | 1.0 |
| 2 — Fade Extreme | 0.75 (per scale) |
| 3 — Return to Value | 1.0 |
| 4 — VWAP Bounce | 1.25 |

### Circuit breaker thresholds (retuned for intraday)

| Tier | Threshold | Action |
|---|---|---|
| L1 | −1.5% intraday | Halve all new position sizes |
| L2 | −2.5% intraday | Block new entries; manage existing |
| L3 | −5% peak-to-valley over rolling 5 days | Close all, write `lock.file`, exit |

### Open-position management

State machine per open position, evaluated each bar close:

```
OPEN → STOPPED      bar trades through stop
OPEN → TARGETED     bar trades through target
OPEN → BREAKEVEN    +1R reached → move stop to entry
OPEN → TIME_STOP    held > max_hold_bars (default 12 = 1h on 5min)
```

- **Equity:** broker-side native bracket orders (entry + stop + target).
- **Crypto:** engine-managed virtual stops (Alpaca crypto lacks full bracket support).
- **Reconciliation:** every wake compares broker state vs `position_book`; mismatch logs `RECONCILE_DRIFT` and flattens conservatively.

## 7. Backtest Engine

### Core invariant
Backtest replays historical bars through the **same** `SessionContext`, `Setup`, `EntryFilter`, and `position_book` classes used live. The only swaps are the bar source (`alpaca_data` ↔ `intraday_replay`) and the order sink (`AlpacaClient` ↔ `SimulatedFillEngine`).

### Fill model (conservative defaults)

- **Limit orders** fill if next bar's range touches the limit; fill price = limit (no improvement).
- **Stop orders** fill at stop ± slippage; gap-through fills at gap price + slippage.
- **Market orders** (Setup 2 scale-ins) fill at next bar's open + slippage.
- **Slippage:** `slippage_bps` per asset class.
- **Commissions:** equity 0 (Alpaca commission-free); crypto 25 bps to model spread.
- **Within-bar ordering:** stops/targets fire **before** new entries (prevents same-bar entry+stop "free" wins).
- **No partial fills** in v1.

### Output

```
BacktestResult:
  trades:       DataFrame   one row per closed trade
  equity_curve: Series
  per_setup:    {setup → {trades, win_rate, expectancy_R, profit_factor}}
  per_symbol:   {symbol → {trades, total_pnl, ...}}
  per_regime:   {regime → {trades, win_rate}}
  filter_audit: {filter → count_rejected}
  metrics:      {sharpe, sortino, max_dd, cagr, calmar, hit_rate, avg_R,
                 max_consec_losses}
```

### Bars cache

- Cache to `runtime/bars_cache/{symbol}_{timeframe}_{start}_{end}.parquet`.
- Same wrapper (`alpaca_data.get_bars`) used live and in backtest; backtest preferentially reads cache.

### Three sanity tests (must pass before backtest is trusted)

1. **No-signal universe** — bars where no setup ever fires; equity curve flat.
2. **Idempotency** — two runs with same inputs are bit-identical.
3. **Live-equivalence smoke** — replay yesterday's live trades through backtest; fills match within 1 bp.

## 8. Sessions, News, Asset Classes

### `SessionContext` shape

```python
@dataclass
class SessionContext:
    symbol: str
    asset_class: str                     # "equity" | "crypto"
    session_start_ts: datetime
    bars: list[Bar]
    vwap: float
    sigma_upper: float
    sigma_lower: float
    day_high: float
    day_low: float
    avg_range_20d: float
    regime: str                          # "Range" | "Discovery" | "Trend" | "Undefined"
    touch_counts: dict[float, int]       # level decay (Principle 5)
```

Context resets at the asset class's session boundary. If the engine restarts mid-session, context is rebuilt from `alpaca_data.get_bars(since=session_start)` — reset is idempotent.

### News blackouts

Config-driven for v1:

```yaml
news_blackouts:
  - { start: "2026-05-15T08:30:00-04:00", duration_min: 10, label: "CPI" }
  - { start: "2026-05-21T14:00:00-04:00", duration_min: 15, label: "FOMC" }
```

`NewsBlackoutFilter` rejects entries when `now ± 5min` overlaps any window.

## 9. Configuration Schema (`config/settings.yaml`)

```yaml
system:
  name: vwap_wave
  version: "2.0.0"
  trading_env: paper            # paper | live | backtest

scheduler:
  bar_timeframe: "5Min"
  wake_grace_seconds: 5
  poll_fallback_seconds: 60

vwap:
  sigma_bands: 1.0
  min_session_bars: 6

acceptance:
  consecutive_closes: 2
  min_distance_atr: 0.25

regime_detector:
  trend_day_range_mult: 1.5
  trend_day_in_value_max: 0.30
  balance_day_in_value_min: 0.60

setups:
  price_discovery:
    enabled: true
    atr_mult_stop: 1.0
    target_R: 1.5
    arm_window_bars: 6
    cooldown_bars: 12
  fade_extreme:
    enabled: true
    atr_mult_stop: 0.75
    scale_weights: [0.4, 0.35, 0.25]
    scale_offsets_atr: [0.0, 0.25, 0.50]
    target: vwap
    cooldown_bars: 12
  return_to_value:
    enabled: true
    atr_mult_stop: 1.0
    target: vwap
    arm_window_bars: 6
    cooldown_bars: 12
  vwap_bounce:
    enabled: true
    atr_mult_stop: 1.25
    target_R: 2.0
    arm_window_bars: 4
    cooldown_bars: 8

risk:
  max_risk_per_trade: 0.005
  max_notional_per_trade_pct: 0.20  # cap notional at 20% of equity per trade
  max_concurrent_positions: 4
  max_daily_risk_open: 0.02
  consecutive_loss_limit: 2
  loss_filter_scope: per_symbol     # per_symbol | system_wide
  circuit_breaker:
    daily_loss_limit_1: 0.015
    daily_loss_limit_2: 0.025
    drawdown_limit: 0.05

filters:
  opening_blackout_min: 15
  volume_deficit_pct: 0.30

position_management:
  max_hold_bars: 12
  breakeven_at_R: 1.0
  trail_at_R: 1.5
  trail_atr: 1.0

asset_classes:
  equity:
    timezone: "America/New_York"
    session_open_local: "09:30"
    session_close_local: "16:00"
    slippage_bps: 2
    commission_per_share: 0.0
    symbols:
      - SPY
      - QQQ
      - IWM
      - XLF
      - XLE
      - GLD
      - AAPL
      - MSFT
      - NVDA
      - TSLA
      - AMZN
      - GOOGL
      - META
      - AMD
      - JPM
      - AVGO
      - NFLX
      - COIN
      - PLTR
      - UBER
  crypto:
    timezone: "UTC"
    session_open_local: "00:00"
    session_close_local: "23:59"
    slippage_bps: 5
    commission_bps: 25
    symbols:
      - "BTC/USD"
      - "ETH/USD"
      - "SOL/USD"
      - "AVAX/USD"
      - "LINK/USD"

news_blackouts: []

backtest:
  start: "2024-01-01"
  end: "2026-04-30"
  initial_equity: 100000
  cache_dir: "runtime/bars_cache"

broker:
  paper_trading: true
  handshake_symbol: SPY

logging:
  level: INFO
  log_file: logs/vwap_wave.log
```

## 10. Migration Plan

### Phases (commit per phase)

```
Phase 0  Scaffold + delete dead code
Phase 1  Data layer:        alpaca_data.py, BarClock, bars cache
Phase 2  Session & VWAP:    SessionContext, vwap, acceptance, regime_detector
Phase 3  Setups:            base_setup + 4 setup state machines + unit tests
Phase 4  Risk pipeline:     filters, sizing, daily_ledger, position_book
Phase 5  Live engine:       scheduler/loop, modified main.py, broker extensions
Phase 6  Backtest engine:   intraday_replay, SimulatedFillEngine, performance metrics
Phase 7  Dashboard:         new ui/dashboard.py panels
Phase 8  Validation:        the three backtest sanity tests
Phase 9  Config + docs:     new settings.yaml, README rewrite
```

### File-by-file action

| Path | Action |
|---|---|
| `engine/hmm_model.py` | delete |
| `engine/regime_classifier.py` | delete |
| `engine/__init__.py` | delete dir |
| `core/feature_eng.py` | delete |
| `core/orchestrator.py` | delete |
| `core/portfolio.py` | rewrite → `state/position_book.py` + `state/daily_ledger.py` |
| `strategies/vol_allocation.py` | delete |
| `strategies/base_strategy.py` | rename → `strategies/base_setup.py` |
| `backtest/walk_forward.py` | delete |
| `backtest/benchmarks.py` | keep |
| `backtest/performance.py` | rewrite (per-trade R metrics) |
| `core/data_loader.py` | rewrite → `broker/alpaca_data.py` |
| `risk/circuit_breakers.py` | keep (retune defaults) |
| `risk/manager.py` | rewrite (façade over filters + sizing) |
| `broker/alpaca_client.py` | extend (bars, crypto endpoints, brackets) |
| `broker/order_executor.py` | modify (bracket orders, virtual stops) |
| `ui/dashboard.py` | rewrite |
| `ui/logging_setup.py` | keep |
| `main.py` | rewrite (scheduler boot) |
| `config/settings.yaml` | rewrite |
| `tests/` | add (unit + integration) |
| `README.md` | rewrite |
| `requirements.txt` | add `pytz`, `pandas-market-calendars`; remove `hmmlearn`, `yfinance` |

### Test strategy

| Type | File | Asserts |
|---|---|---|
| Unit | `test_vwap.py` | Incremental VWAP matches batch calc |
| Unit | `test_acceptance.py` | N-close + distance threshold logic |
| Unit | `test_regime.py` | Range/Trend/Discovery on canned bars |
| Unit | `test_setup_*.py` | State transitions per setup |
| Unit | `test_filters.py` | Each filter rejects/passes correctly |
| Unit | `test_sizing.py` | ATR sizing math; cap enforcement |
| Unit | `test_position_book.py` | Reconciliation against simulated broker |
| Integ | `test_backtest_smoke.py` | One symbol, one week, end-to-end |
| Integ | `test_live_smoke.py` | Alpaca paper handshake on SPY |

Targets: unit suite <5s, full <30s.

### Risks & rollback
- **Alpaca rate limits:** 25 symbols × 1 call / 5 min ≈ 300/h, well under 200/min. Bars cache mitigates backtest load.
- **Crypto symbol semantics:** `BTC/USD` (v1beta3) vs `BTCUSD` (legacy); `alpaca_data.py` normalizes.
- **No bracket orders on crypto:** engine-managed virtual stops.
- **Rollback:** branch isolated from `main`; lock-file mechanism preserves any half-deployed live state.

## 11. Definition of Done (gate before live)

1. All unit + integration tests green.
2. Backtest 2024-01-01 → 2026-04-30 produces a complete report (gate is "completes without errors", not profitability).
3. Live-equivalence smoke test passes in paper.
4. One full session reviewed manually in paper mode; dashboard shows correct setup/filter audit.
5. `system.trading_env` stays at `paper`; flipping to `live` is a separate follow-up PR after expectancy is established in paper.

## 12. Open Questions (deferred)

- Worktree vs in-place implementation — decided at execution time.
- Profitability tuning (per-setup expectancy, optimal R targets, regime weights) — explicitly out of v1; v1 ships the framework with reasonable defaults; tuning happens via the per-setup/per-regime breakdown in backtest output.
- Live trading enablement — separate v1.1 PR once paper expectancy is established.
- Economic-calendar API integration — v2.
