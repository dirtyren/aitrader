# Opening Drive — Equity Day-Trade Strategy (Design)

**Date:** 2026-08-28
**Status:** Approved design, not yet implemented
**Strategy name:** `opening_drive_equity_trader`
**Asset class:** equity (long-only, v1)

## 1. Purpose

Each session, screen a liquid US equity universe using the first 30 minutes of
the NYSE regular session (09:30–10:00 NY), select the strongest day-trade
candidates, enter them on a confirmed continuation trigger, and hold nothing
past 15:30 — 30 minutes before the close.

This is a *screened* intraday momentum strategy: the symbol list is discovered
daily rather than configured. It is distinct from the existing `orb_vwap` setup
(fixed symbol list) and from `gap_and_go` (pre-market screen, entry before the
open).

## 2. Binding constraints

These constraints shaped the design and must be re-examined if any changes.

### 2.1 Market data is IEX-only

`broker/alpaca_client.py` hardcodes `"feed": "iex"`. The account has the free
Alpaca data plan. IEX carries roughly 2% of consolidated US equity volume.

**Consequence:** absolute cross-sectional comparisons are invalid. AAPL's IEX
volume is not comparable to XOM's IEX volume, because their IEX market shares
differ. Every screening metric MUST be **self-normalized** — either a symbol
measured against its own history, or a ratio taken within a single feed — so
that the ~2% share factor cancels.

A per-symbol IEX share is stable over weeks, which is what makes a
`today / trailing-20-day` ratio meaningful even though the level is not.

### 2.2 Universe restricted to liquid names

Because IEX may print nothing at all in a given minute for a thin symbol, the
universe is limited to S&P 500 + Nasdaq-100 (~515 unique symbols after
overlap). Every member is liquid enough that IEX prints in essentially every
minute. A `bar_coverage` gate (§4.2) enforces this per symbol per day rather
than trusting the universe definition.

Broader universes (full Nasdaq listing, ~3,800 symbols) were rejected: the
majority would have sparse or absent IEX prints, making their opening range and
relative volume noise rather than signal.

### 2.3 No bulk market-data path exists

`AlpacaClient.get_stock_bars` and `AlpacaData.get_bars` are single-symbol.
Fetching 515 symbols one call at a time at 10:00 is not viable. A multi-symbol
method is required (§6).

### 2.4 Pattern Day Trader rule

The strategy opens and closes every position intraday, generating 4+ day trades
per session. In a US margin account under $25,000 equity, FINRA limits this to
3 day trades per rolling 5 business days; a breach restricts the account to
closing-only for 90 days.

The target account is **paper** for now, and paper accounts do not enforce PDT.
The design therefore assumes the unconstrained case (margin, >$25k) and adds an
explicit, tested PDT guard that blocks live startup when equity is below the
threshold — so the constraint is caught deliberately rather than discovered in
production.

## 3. Capital sharing with `sma_slope_equity_trader`

`trader-opening-drive-equity` ships **active alongside** the existing
`trader-sma-slope-equity` service. Both trade the same Alpaca equity account.

`sma_slope_equity_trader` is all-in by design: `max_notional_per_trade_pct:
0.95`, `max_concurrent_positions: 1`, no time-boxed exit (`max_hold_bars:
10000`), holding TQQQ until the SMA flips — i.e. for days or weeks.

`main.py:604` feeds `non_marginable_buying_power` (falling back to `cash`) into
`RiskManager.update_cash`, and `risk/sizing.py:size_position` caps every
position at `available_cash * (1 - cash_buffer_pct)`. With 95% of notional
committed, non-marginable buying power is near zero, so `size_position` would
return `qty=0` and every signal would be rejected with `"sized to zero"` — the
strategy would silently take no trades on any day sma-slope is long, while
appearing simply to have found no candidates.

**Resolution:** reduce `sma_slope_equity_trader`'s
`max_notional_per_trade_pct` from `0.95` to `0.60`, reserving ~40% of the
account for Opening Drive.

### 3.1 Sizing consequence — which limit is load-bearing

With ~40% of capital available and 5 concurrent positions, per-position
notional is capped near 7–8%. Intraday stops on these setups run roughly 1–2%
of price, so risk-based sizing (`max_risk_per_trade: 0.005`) would ask for far
more notional than the cap allows.

Therefore **the notional cap binds, not the risk cap.** Effective risk lands
near 0.1% per trade and ~0.5% per day. `max_daily_risk_open` will never
trigger. This is a deliberately conservative configuration; the config file
must document which limit is actually load-bearing so the inert ones are not
mistaken for active protection.

**Required:** unlike `main_gap_and_go.py` (which never calls `update_cash`,
leaving `available_cash=None` and skipping the cash cap entirely), this loop
MUST call `update_cash` on every equity refresh, so the cap binds by decision
rather than by omission.

## 4. Screening

### 4.1 Metrics

Baselines are computed nightly per symbol (§5, 16:10 phase). Live values come
from the 30 one-minute bars covering 09:30–10:00.

| Metric | Formula | Captures |
|---|---|---|
| `rvol_or` | `or_volume / avg_or_volume_20d` | Participation — the most robust "something is happening here" signal |
| `disp_atr` | `(or_close - prev_close) / atr_14d` | Displacement in volatility units, comparable across price levels |
| `or_width_atr` | `(or_high - or_low) / atr_14d` | Energy, evaluated as a band — too narrow means no move, too wide means the move is over and the stop must be uneconomic |
| `clv` | `(or_close - or_low) / (or_high - or_low)` | Close location — finishing near the OR high means buyers held control into the cut |
| `rs_atr` | `(sym_or_return - spy_or_return) / (atr_14d / prev_close)` | Idiosyncratic strength — without it, a broad +1% SPY morning produces five correlated longs |
| `above_vwap` | `or_close > or_vwap` | Session control; same concept already in `setup_orb_vwap.py` |
| `bar_coverage` | `bars_with_volume / 30` | **IEX data-quality gate** — if IEX printed in only 24 of 30 minutes, that symbol's OR high/low is fiction |

Every metric is self-normalized per §2.1. `or_vwap` is computed from IEX
price×volume within the OR window, so the feed factor cancels in the ratio.

**SPY must be appended to the bulk request explicitly.** `rs_atr` needs the
benchmark's opening-range return, but SPY is an ETF and therefore not a member
of either index — it will not appear in the universe CSV. The scanner requests
`universe + ["SPY"]` and treats SPY as benchmark-only: it supplies
`spy_or_return` and is never itself a candidate. If SPY's bars are missing or
fail `bar_coverage`, `rs_atr` is uncomputable and the scanner MUST return an
empty watchlist rather than fall back to an unbenchmarked ranking — a
market-wide move would otherwise be mistaken for five independent stock
signals, which is the exact failure `rs_atr` exists to prevent.

### 4.2 Gates

All must pass. Values are v1 starting points, to be swept (§8).

| Gate | Threshold |
|---|---|
| `or_close` | `>= 5.00` |
| `avg_daily_volume_20d` | `>= 100_000` **(IEX-denominated — see note)** |
| `bar_coverage` | `>= 0.90` |
| `rvol_or` | `>= 2.0` |
| `disp_atr` | `>= 0.5` |
| `or_width_atr` | `0.4 <= x <= 2.0` |
| `clv` | `>= 0.6` |
| `above_vwap` | `true` |
| `rs_atr` | `> 0` |

**The ADV gate is IEX-denominated, not consolidated.** This is a trap worth
stating plainly: `gap_and_go` uses `min_avg_daily_volume: 1_000_000`, which
reads as a consolidated-volume threshold. But the baselines are built from the
IEX feed, where a typical S&P 500 name showing ~3M consolidated shares prints
only ~60–100k on IEX. A 1,000,000 threshold against IEX volume would reject
substantially the entire universe and the scanner would return nothing, every
day, without erroring. The threshold is therefore set at `100_000` IEX shares.

Note also that the gate is close to redundant by construction: S&P 500 +
Nasdaq-100 membership already guarantees liquidity, and `bar_coverage` catches
per-day print sparsity directly. The gate is retained as a cheap backstop
against a stale or malformed universe file, not as the primary liquidity
control.

Long-only in v1: negative `disp_atr` candidates are dropped rather than
inverted.

### 4.3 Ranking

```
score = rvol_or * rs_atr
```

Take the top `ceil(max_concurrent_positions * 1.5)` as the day's watchlist.

Two factors in the rank with everything else as a gate — deliberately mirroring
`GapScanner.run_cut`'s `gap_atr_mult * rvol`, so the existing
`scripts/sweep_equity_strategy.py` harness applies unchanged and no new
overfitting surface is introduced.

## 5. Daily lifecycle

All times America/New_York; internal timestamps are timezone-aware UTC.

| Time | Phase |
|---|---|
| 16:10 (prior session) | **Baseline refresh** — 515 symbols x 20 sessions of 09:30–10:00 volume, plus daily ATR(14) and ADV(20) → `baselines.json`. Post-close so it never competes with the cut. |
| 09:00 | Boot, validate broker connection, baseline staleness check (refresh here only as fallback) |
| 09:30–10:00 | Opening range forms. **System idle — no requests issued.** |
| 10:00 | **Cut.** One bulk multi-symbol bars request (`09:30→10:00`, `1Min`) → compute metrics → gates → rank → watchlist |
| 10:00–11:00 | **Entry window.** 1-min bars for watchlist symbols only (~8). Each setup waits for its trigger. |
| 11:00 | Entry window closes; un-triggered setups disarm |
| 11:00–15:30 | Managed phase; `PositionManager` on 5-min bars |
| 15:30 | Force-close every open position (§7.2) |
| 15:35 | Day reset |

**No polling loop.** `gap_and_go` polls snapshots across its universe every 5
minutes; this strategy does not need to. The entire opening range is available
in a single bulk request at 10:00, which is simpler, cheaper, and immune to the
partial-state bugs that missed polls cause.

The 16:10 baseline job runs **inside the trader process** as a post-close
phase, not as a separate compose service or cron entry — the process is already
running and already holds a broker connection, so a separate service would add
surface for no benefit.

## 6. Modules

Mirrors the `gap_and_go` split so the shapes are familiar.

| Path | Responsibility |
|---|---|
| `strategies/opening_drive_scanner.py` | `OpeningDriveScanner` — universe, baselines, `compute_metrics()`, `run_cut()`. Pure logic, no network, same testability contract as `gap_scanner`. |
| `strategies/setup_opening_drive.py` | `OpeningDriveSetup` — arms from a `ScanResult`, watches 1-min bars for the trigger, emits `SetupSignal`. |
| `scheduler/opening_drive_loop.py` | Phase handlers, each independently testable without network or sleep. |
| `main_opening_drive.py` | Production wiring. |
| `config/settings_opening_drive_equity.yaml` | Strategy config. |
| `config/universe_sp500_ndx100.csv` | `symbol,sector` — built by `scripts/build_universe_sp500_ndx100.py`. |
| `scripts/build_opening_drive_baselines.py` | The 16:10 baseline job. |
| `broker/alpaca_client.py` | **New:** `get_stock_bars_multi()` — multi-symbol `/v2/stocks/bars`, paginated. |
| `broker/alpaca_data.py` | **New:** `get_bars_multi()` — cached wrapper. |
| `risk/filters.py` | **New:** `SectorExposureFilter`. |

## 7. Risk and execution

### 7.1 Portfolio risk layer

Reused unchanged:

- `risk/sizing.py:size_position` — `max_risk_per_trade: 0.005`,
  `max_notional_per_trade_pct: 0.07` (see §3.1)
- `ConcurrentPositionFilter(max_concurrent=5)` — the top-N cap
- `RiskBudgetFilter(daily_open_risk_cap_pct=0.025)` — inert given §3.1, kept
  for config-shape consistency and documented as such

New and changed:

- **`SectorExposureFilter(max_per_sector=2)`** — reads the sector map from the
  universe CSV, counts open positions in the signal's sector, rejects beyond
  the cap. Without this, "top-5 at full risk each" can become one leveraged
  sector bet: five semiconductor longs are one risk, not five.
- **`ConsecutiveLossFilter` scope must be `system_wide`, not `per_symbol`.**
  Every other strategy here trades a fixed symbol list, where `per_symbol` is
  correct. This strategy rotates symbols daily and will rarely see the same
  name twice, so `per_symbol` would never fire — dead config offering false
  comfort.

  **Correction (2026-08-28, during implementation planning):** an earlier
  revision of this section specified `per_strategy`. **No such scope exists
  in the code.** `ConsecutiveLossFilter.check` branches on the exact string
  `"system_wide"` (reading `ledger.consec_losses_system`) and treats every
  other value as per-symbol — so `per_strategy` would have silently fallen
  through to the per-symbol path and never fired, producing exactly the dead
  config this requirement exists to avoid. `system_wide` is the value that
  delivers the intent.
- **PDT guard** — boot-time precondition, config-gated, blocking live start
  when the account is margin and equity < $25,000. Tested, not assumed (§2.4).

**Slot allocation is first-come-first-served among triggers, not by rank.**
Because entries are trigger-based they arrive at different times, so a rank-7
name triggering at 10:05 takes a slot from a rank-1 name triggering at 10:40.
This is accepted for v1: trigger timing is itself information — an earlier,
cleaner trigger is usually the better trade — and reserving slots for
high-ranked names means deliberately idling capital on a setup that may never
arrive. Recorded here as an explicit, tested decision rather than an accident
of arrival order.

### 7.2 Entry trigger

One trigger in v1, long-only:

> **OR-high reclaim.** After 10:00, price pulls back off `or_high`; then a
> 1-min bar closes above `or_high` with volume >= `volume_confirm_mult` x the
> trailing bar average, and `close > session_vwap`. Stop at the pullback low.
> Target `entry + target_R * R` where `R = entry - stop`.

A VWAP-retest variant was considered and deferred. This trigger is
structurally what `setup_orb_vwap.py` already implements (range-high break,
above VWAP, positive VWAP slope), so the setup code, sweep harness, and
backtest replay already understand its shape. Building both variants now would
double the parameter surface before any evidence exists that either works.
VWAP-retest is the first post-baseline sweep candidate (§8).

Stops sit at market structure (the pullback low), not at `entry - k*ATR`. This
is what makes R meaningful and what makes "respect account size risk"
enforceable: the stop is a level the market must actually violate.

### 7.3 Exit stack

Reused: bracket OCO submitted at entry (`submit_bracket_order`), then
`PositionManager` for `breakeven_at_R: 1.0`, `trail_at_R: 1.5`,
`trail_atr: 1.0`.

`max_hold_bars: 36` on 5-min bars (3 hours), **counted from the position's
first managed-phase bar at 11:00**, not from entry. Entries occur between 10:00
and 11:00 on 1-min bars, so counting from entry would make the time stop depend
on trigger timing; anchoring at 11:00 gives every position the same 11:00→14:00
window regardless of when it filled. Note 11:00→15:30 is 54 bars, so any value
>= 54 would be inert. The 15:30 flatten remains the unconditional backstop.

**Required, tested ordering at 15:30:** cancel the live OCO child orders
**before** submitting the market close. Flattening while the stop and target
legs remain open leaves orphaned orders — exactly the failure class the
reconciler exists to clean up. `broker/safe_close.py` provides the mechanism.

## 8. Integration

**Dashboard: no code changes required.** `ui/data/strategy_configs.py:141`
discovers strategies by globbing `config/settings*.yaml` and filters via
`list_by_asset_class` on the `asset_classes` block. Creating
`config/settings_opening_drive_equity.yaml` with a unique `system.name` and an
`asset_classes.equity` key makes the strategy appear in the Strategies, Live,
Settings, and Config tabs automatically. The config must carry
`system.{name,version,trading_env}`, `asset_classes.equity`, `risk`, and a
`setups` block with `enabled:`.

The parser reads `symbols` from the asset-class block; this strategy has no
static symbol list (same as `gap_and_go`, whose config notes "symbols list
intentionally absent — populated dynamically by scanner"). The precedent
exists, but §9 requires a test that the dashboard renders an empty-`symbols`
config cleanly rather than trusting it.

**Reconciler: no changes required.** `reconciler-equity` reconciles
broker-vs-MySQL per asset class, not per strategy. The new strategy writes to
the same tables and is covered as soon as it trades.

**Docker:** add `trader-opening-drive-equity`, **active**, alongside
`trader-sma-slope-equity`, following the existing service pattern
(`command: python main_opening_drive.py --config
config/settings_opening_drive_equity.yaml`). Also apply the
`max_notional_per_trade_pct` reduction from §3.

## 9. Testing

Unit tests follow `tests/test_gap_and_go_loop.py` — inject providers, no
network, no sleeps.

**Scanner:** each metric formula against hand-computed fixtures; one test per
gate in §4.2 proving it rejects a candidate that fails only that gate; ranking
order; `bar_coverage` rejection of a sparse-print symbol; stale-baseline
refusal to cut.

**Setup:** trigger state machine — arms from a `ScanResult`; fires on a valid
reclaim; does **not** fire without volume confirmation; does not fire when
`close <= vwap`; disarms at 11:00.

**Risk:** `SectorExposureFilter` rejects a third same-sector signal;
`ConsecutiveLossFilter` at `per_strategy` scope fires across differing symbols;
PDT guard blocks a sub-$25k margin account and permits paper.

**Sizing:** with `available_cash` reflecting a 60%-committed account, positions
size to the notional cap rather than to zero — the §3 regression, pinned.

**Exit:** 15:30 flatten cancels OCO children before submitting the market
close.

**Integration:** loop phases driven end-to-end with fixture bars; dashboard
renders an empty-`symbols` strategy without error.

## 10. Known risks and limitations

1. **Alpaca free-plan 1-min history depth is unverified.** Six months of
   backtest needs ~515 symbols x 126 sessions x 30 OR bars (~1.95M bars) plus
   full-day bars for daily candidates — roughly 2.4M bars, a few hundred MB
   cached, well within reach of multi-symbol requests plus
   `scripts/cache_bars_universe.py`. But how far back the free IEX plan serves
   1-minute historical bars has **not** been confirmed, and it caps the
   validation window. Verify before committing to a backtest scope.

2. **Survivorship bias is unavoidable and flatters results.** The universe CSV
   is *today's* S&P 500 + Nasdaq-100 membership. Backtesting against it
   excludes names dropped for underperformance — and in a momentum strategy
   those are precisely the names that broke down. Point-in-time constituent
   data is a paid dataset. Accepted and documented; keep validation windows
   short enough that membership drift is small, and read results knowing they
   are optimistic.

3. **IEX-derived opening ranges are inside consolidated ranges.** `or_high` and
   `or_low` from IEX-only prints will be marginally narrower than the true
   consolidated range. `bar_coverage` bounds the error but does not eliminate
   it. Live fills will differ from backtest fills more than for a
   consolidated-feed strategy.

4. **Gate thresholds in §4.2 are unvalidated priors.** They are starting
   points for `scripts/sweep_equity_strategy.py`, not tuned values. First
   sweeps: `rvol_or`, `disp_atr`, `clv`, and the `or_width_atr` band; then the
   deferred VWAP-retest trigger as a variant.

5. **Two strategies share one account.** Even at 60/40 the two contend for
   buying power during the day, and a drawdown affects both simultaneously.
   Per-strategy Alpaca credentials are not supported today (the multi-account
   design keys on asset class, not strategy).

## 11. Deferred

- Short side (scanner and setup carry a `side` field; gates would invert)
- VWAP-retest trigger variant
- Rank-weighted position sizing (no evidence rank is calibrated enough)
- Slot reservation for high-ranked candidates (§7.1 chose FCFS)
- Point-in-time universe constituents
