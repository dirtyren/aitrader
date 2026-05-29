# Gap-and-Go Equity Strategy — Design

**Status:** Design approved, awaiting implementation plan
**Date:** 2026-05-29
**Owner:** Alessandro Ren
**Scope:** New equity strategy `gap_and_go`, deployed as its own trader container `trader-alpaca-gap_and_go`

## Goals

Add a Gap-and-Go strategy to the equity portfolio. The strategy enters during pre-market on stocks gapping above their previous close on abnormal volume, when price breaks the pre-market high. The strategy fills a gap in the existing portfolio (which is dominated by intraday auction-theory and RSI mean-reversion setups, all keyed off the regular session) by trading overnight catalyst-driven moves.

## Non-Goals (v1)

- **Shorts.** Long-only in v1. Gap-down shorts have different risk (squeeze, locate availability, hard borrows). Revisit after v1 is profitable.
- **Backtest.** Existing backtest infra does not model pre-market. v1 is validated by paper trading only.
- **Automatic universe maintenance.** Russell 1000 CSV is manually refreshed quarterly.
- **Re-entry.** Once a position closes for any reason, that symbol is done for the day.
- **News blackout filtering.** Gap-and-Go's edge *is* news/catalysts. Filtering them out would defeat the strategy.
- **Scheduler refactor.** Gap-and-Go runs as its own entrypoint and loop. We do not retrofit the existing engine to support dynamic symbols.

## Architecture & Daily Lifecycle

New container `trader-alpaca-gap_and_go` running its own process, mirroring the per-strategy container pattern (see existing `trader-alpaca-orb`, `trader-alpaca-rsi`, etc.).

All times America/New_York:

| Time | Action |
|---|---|
| 03:30 | Container boots, validates Alpaca connection |
| 03:35 | Refresh RVOL/ATR baselines if stale (>7 days) for full Russell 1000 |
| 04:00 | Pre-market opens. Snapshot poll loop starts (every 5min) over the universe |
| 08:30 | **Scanner cut.** Apply filters, rank candidates, register top N as the day's dynamic watchlist |
| 08:30 → 09:30 | Per-symbol 1-min bars on watchlist. `GapAndGoSetup` watches for pre-market high break + volume confirmation. Entries are extended-hours limit orders. Stop/target tracked client-side |
| 09:30 | Regular session opens. For each filled pre-market position, attach OCO (stop + target). From here positions are managed by existing `PositionManager` (breakeven, VWAP trail, etc.) — see Bar Timeframe note below |
| 15:55 | Force-close any remaining Gap-and-Go positions (EOD flat) |
| 16:00 | Regular session closes |
| 16:05 | Loop back to idle until next 03:30 boot |

### Invariants

- Symbols are not known until 08:30. `build_setups()` runs once per qualified symbol after scanner cut, not at startup.
- One position per symbol per day. No re-entry.
- No overnight holds. The alpha is the gap, not the trend.
- Existing risk/circuit-breaker layer applies unchanged.

### Bar timeframe transition (08:30 → 09:30 → 16:00)

Pre-market entry detection requires 1-min bars (you can't see a high-break clearly on 5-min granularity). Regular-session position management uses 5-min bars to match all other equity traders' `PositionManager` behavior. The `gap_and_go_loop` is responsible for switching the per-symbol bar subscription at 09:30:00 ET — flushing the 1-min `SessionContext` and rebuilding it on 5-min bars from `09:30` onward. Positions filled in pre-market keep their original entry/stop/target; only the bar feed changes.

## Scanner & Universe Cache

### New module: `strategies/gap_scanner.py`

Owns the universe and the daily candidate selection.

### Universe seed: `config/universe_russell_1000.csv`

~1000 symbols, one per line, checked into the repo. Manually refreshed quarterly.

### Baseline cache: `runtime/gap_scanner/baselines.json`

Refreshed weekly (or on first boot when stale > `baselines_max_age_days`):

```json
{
  "AAPL": {
    "atr_14d": 3.42,
    "avg_premarket_volume_20d": 850000,
    "avg_daily_volume_20d": 52400000,
    "computed_at": "2026-05-23T03:35:00Z"
  }
}
```

Refresh fetches 25 daily bars per symbol (cached via existing `AlpacaData`) plus ~25 days of 1-min pre-market bars per symbol. Cost: ~2000 API calls weekly, well under Alpaca data-API limits.

### Candidate state (04:00–08:30)

`GapScanner.candidate_status(ts)` polls Alpaca's bulk snapshot endpoint every 5 minutes for the full universe and tracks running pre-market high/low/volume per symbol. No bar-level data needed during this window.

### Cut at 08:30

`GapScanner.run_cut(ts)` applies all filters (every one must pass):

- `last_price >= min_price` (default $5)
- `avg_daily_volume_20d >= min_avg_daily_volume` (default 1M)
- `premarket_volume / avg_premarket_volume_20d >= min_rvol` (default 5.0)
- `abs(gap_pct) >= min_gap_pct` (default 4.0)
- `abs(gap_dollars) / atr_14d >= min_gap_atr_mult` (default 1.5)
- Tradable on Alpaca, `gap_pct > 0` (long-only in v1)

Ranks by `abs(gap_atr_mult) * rvol`, descending. Returns top N where `N = max_concurrent_positions * candidate_multiplier` (default 1.5).

`ScanResult` (frozen dataclass): `symbol, gap_pct, gap_atr_mult, rvol, premarket_high, premarket_low, premarket_vwap, last_price, atr_14d, side, cut_ts`.

### Failure modes

- **Stale baselines (>14 days, i.e. 2× `baselines_max_age_days`):** scanner refuses to run, alerts via existing logging. Will not trade on bad reference data. Refresh is attempted on every boot when `now - computed_at > baselines_max_age_days` (default 7); the 2× threshold is the hard fail-safe if refresh keeps failing.
- **Empty candidate list at 08:30:** log + sleep until tomorrow.
- **Snapshot failure mid-window:** retain last successful snapshot, log warning, continue. If zero successful snapshots by 08:30, abort the day.

## Setup State Machine

### New strategy: `strategies/setup_gap_and_go.py`

`BaseSetup` subclass, one instance per qualified symbol per day.

### States

```
IDLE → FILLED → MANAGED → CLOSED
   │
   └─→ EXPIRED  (entry deadline passed without trigger)
```

- **IDLE:** waiting for pre-market high break (08:30 → entry deadline). Setup tracks running pre-market high/low and updates them on each 1-min bar.
- **FILLED:** entry submitted, fill confirmed, client-side stop/target tracking until 09:30.
- **MANAGED:** OCO attached at 09:30, behaves like any other intraday position.
- **CLOSED:** terminal state (stop / target / trail / EOD).
- **EXPIRED:** entry deadline passed without trigger; no entry today.

(The base `BaseSetup` class exposes a generic `ARMED` concept used by other strategies for two-stage triggers. Gap-and-Go uses a single trigger — the high-break — so it transitions IDLE → FILLED directly. The `state` attribute on `BaseSetup` will hold `"IDLE"` until entry, then `"FILLED"`, etc.)

### Entry trigger

Per 1-min bar close in 08:30 → entry deadline (default 09:30):

1. If `bar.high > self.premarket_high`, update `self.premarket_high` and skip the bar (never chase a same-bar new high).
2. Compute 5-bar trailing average volume.
3. Trigger fires when:
   - `bar.close > self.premarket_high`, AND
   - `bar.volume >= volume_confirm_mult * avg_recent_vol` (default 2.0×)
4. Slippage guard: if `(entry - premarket_high) / premarket_high > max_entry_slippage_pct / 100` (default 0.5%), reject and wait for next bar.
5. `stop = max(premarket_low, entry - atr_mult_stop_cap * atr_14d)` (default cap 2.0×).
6. `target = entry + target_R * (entry - stop)` (default target_R 2.0).
7. Emit `SetupSignal` with `notes={"style": "gap_continuation", "premarket_high": ..., "premarket_low": ..., "extended_hours": True}`.

### Edge cases

- Pre-market high keeps extending past 08:30 cut → state machine updates the level on each new bar before checking break.
- Volume-only fakeout → `volume_confirm_mult` rejects.
- Wide-bar overshoot → slippage guard rejects.
- No break by deadline → state goes to `EXPIRED`.
- Partial fill on pre-market limit → existing position-book logic handles partials; OCO attached to filled qty at 09:30.
- Filled but never reaches 09:30 (early close, holiday) → existing `max_hold_bars` kicks in once OCO attached. Pre-market limit orders use `time_in_force="day"`, so unfilled orders persist into regular session: if they fill after 09:30 but before OCO attach has run for the day, the position is treated as a normal post-09:30 fill and gets bracket-attached on the next loop tick.

## Order Plumbing

All changes to `broker/alpaca_client.py` and `broker/order_executor.py` are **strictly additive**. Default values preserve all existing behavior.

### Change 1: `AlpacaClient.submit_order` — add `extended_hours: bool = False`

```python
def submit_order(self, symbol, qty, side,
                 order_type="market", time_in_force="day",
                 limit_price=None, client_order_id=None,
                 extended_hours=False):       # NEW
    ...
    if extended_hours:
        if order_type != "limit":
            raise ValueError("extended_hours requires order_type='limit'")
        if time_in_force != "day":
            raise ValueError("extended_hours requires time_in_force='day'")
        payload["extended_hours"] = True
    ...
```

### Change 2: `AlpacaClient.attach_oco` — new method

`order_class="oco"` attaches stop + target legs to an existing position (different from `bracket`, which has an entry leg).

```python
def attach_oco(self, symbol, qty, side, stop_price, target_price,
               time_in_force="day", client_order_id=None):
    payload = {
        "symbol": symbol, "qty": qty, "side": side,
        "type": "limit", "limit_price": _round_to_tick(target_price),
        "time_in_force": time_in_force,
        "order_class": "oco",
        "stop_loss": {"stop_price": _round_to_tick(stop_price)},
        "take_profit": {"limit_price": _round_to_tick(target_price)},
    }
    ...
```

### Change 3: `OrderExecutor` — branch on `extended_hours` flag

Existing `submit()` always calls `submit_bracket_order`. New behavior:

- If `signal.notes.get("extended_hours")` → submit plain limit with `extended_hours=True`. Mark the resulting `OpenPosition.pending_oco_attach = True`.
- Else → existing bracket path, unchanged.

### Change 4: `OpenPosition` — add `pending_oco_attach: bool = False`

Tracks pre-market fills awaiting 09:30 OCO submission. Defined wherever `OpenPosition` lives (existing class is in `state/position_book.py`). The flag is set by `OrderExecutor.submit()` when it submits an extended-hours entry, and cleared by `attach_brackets_for_premarket_fills()` after successful OCO submit. Existing call sites do not set the flag, preserving current behavior.

### Change 5: New module `broker/post_open_attach.py`

`attach_brackets_for_premarket_fills(book, executor, now)` — called once at 09:30:00 ET each session by the Gap-and-Go loop, before the first regular-session bar evaluation.

For each position with `pending_oco_attach=True`:
1. Query actual filled qty from broker (in case of partials).
2. Submit OCO via executor.
3. On success: clear `pending_oco_attach`.
4. On failure: submit market close immediately (failsafe — better to flatten than hold naked).
5. Log + alert in both branches.

## Configuration

### New file: `config/settings_gap_and_go_equity.yaml`

```yaml
system:
  name: gap_and_go_equity_trader
  trading_env: paper
  version: 1.0.0

broker:
  handshake_symbol: SPY
  paper_trading: true

asset_classes:
  equity:
    timezone: America/New_York
    session_open_local: '09:30'
    session_close_local: '16:00'
    premarket_open_local: '04:00'
    scanner_cut_local: '08:30'
    entry_window_end_local: '09:30'
    commission_per_share: 0.0
    slippage_bps: 2
    # symbols list intentionally absent — populated dynamically by scanner

scanner:
  universe_file: config/universe_russell_1000.csv
  baselines_path: runtime/gap_scanner/baselines.json
  baselines_max_age_days: 7
  snapshot_poll_seconds: 300
  filters:
    min_price: 5.0
    min_avg_daily_volume: 1_000_000
    min_rvol: 5.0
    min_gap_pct: 4.0
    min_gap_atr_mult: 1.5
  ranking:
    score: gap_atr_mult_x_rvol
    candidate_multiplier: 1.5
  side: long_only

setups:
  gap_and_go:
    enabled: true
    atr_mult_stop_cap: 2.0
    target_R: 2.0
    volume_confirm_mult: 2.0
    max_entry_slippage_pct: 0.5
    entry_window_minutes: 60

position_management:
  breakeven_at_R: 1.0
  max_hold_bars: 36
  trail_at_R: 1.5
  trail_atr: 1.0
  force_close_local: '15:55'

risk:
  max_concurrent_positions: 4
  max_risk_per_trade: 0.005
  max_notional_per_trade_pct: 0.10
  max_daily_risk_open: 0.02
  consecutive_loss_limit: 2
  loss_filter_scope: per_symbol
  circuit_breaker:
    daily_loss_limit_1: 0.015
    daily_loss_limit_2: 0.025
    drawdown_limit: 0.05

scheduler:
  bar_timeframe: 1Min
  regular_session_timeframe: 5Min
  poll_fallback_seconds: 30
  wake_grace_seconds: 5

logging:
  level: INFO
  log_file: logs/gap_and_go_equity_trader.log

news_blackouts: []
```

### New file: `config/universe_russell_1000.csv`

~1000 symbols, one per line. Bootstrap from public Russell 1000 listing; reviewed quarterly.

### New service in `docker-compose.yml`

`trader-alpaca-gap_and_go` mirroring the structure of `trader-alpaca-orb`. Command: `python main_gap_and_go.py`. Volume mount: `./config/settings_gap_and_go_equity.yaml`.

## File Map

New files:
- `strategies/setup_gap_and_go.py`
- `strategies/gap_scanner.py`
- `scheduler/gap_and_go_loop.py`
- `broker/post_open_attach.py`
- `main_gap_and_go.py`
- `config/settings_gap_and_go_equity.yaml`
- `config/universe_russell_1000.csv`
- `tests/test_gap_scanner.py`
- `tests/test_setup_gap_and_go.py`
- `tests/test_post_open_attach.py`
- `tests/test_gap_and_go_loop.py`

Modified files (additive only):
- `broker/alpaca_client.py` — add `extended_hours` to `submit_order`, add `attach_oco`
- `broker/order_executor.py` — branch on `extended_hours` in `submit`
- `state/position_book.py` (or wherever `OpenPosition` is defined) — add `pending_oco_attach`
- `docker-compose.yml` — new service entry

Unmodified (intentionally): `main.py`, `scheduler/loop.py`, all existing strategies, all existing trader configs.

## Testing Strategy

### Unit tests (no network)

- `tests/test_gap_scanner.py` — synthetic snapshot dicts. Edge cases: stale baselines, empty universe, exactly-at-threshold values, snapshot endpoint partial response.
- `tests/test_setup_gap_and_go.py` — synthetic `SessionContext` bars. Coverage: IDLE→signal happy path, slippage rejection, deadline expiry, volume fakeout rejection, pre-market high extension after cut.
- `tests/test_post_open_attach.py` — mocked `OrderExecutor`. Coverage: successful OCO submit, partial-fill quantity reconciliation, OCO rejection → failsafe market close.

### Integration tests (mocked Alpaca, real engine wiring)

- `tests/test_gap_and_go_loop.py` — full day simulation: scanner cut → entry → 09:30 attach → trail → EOD close. Uses `httpx.MockTransport` matching existing `tests/test_alpaca_*.py` style.

### Pre-live verification gate

At least 5 paper-trading days with non-zero positions, no naked positions logged, all OCO attaches successful, before flipping `paper_trading: false`.

## Risks & Open Questions

1. **Snapshot endpoint rate limits:** ~1000 symbols every 5 minutes. Alpaca's data-API plan should handle this comfortably but we'll log per-poll latency and add backoff if observed.
2. **OCO attach race at 09:30:** if Alpaca routing has post-open backlog, `attach_oco` may arrive before the position is visible. Mitigation: 30-second retry loop with exponential backoff, then failsafe market close.
3. **Pre-market liquidity:** limit orders may fill at unfavorable prices. Slippage guard caps this at 0.5% above PMH at decision time, but actual fill price could differ. Acceptable for v1; revisit if observed slippage exceeds 0.3% on average.
4. **Russell 1000 staleness:** quarterly manual refresh is operationally simple but means newly-added high-beta names (e.g. recent IPOs that pop into the index) won't appear until the next refresh. Acceptable for v1.
