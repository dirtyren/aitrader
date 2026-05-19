# Walk-Forward Optimization — Design

**Status:** draft for review
**Author:** brainstormed in-session
**Date:** 2026-05-19
**Targets:** main branch (post Phase 9 of the VWAP Wave migration)

---

## 1. Goal

Add Walk-Forward Optimization (WFO) to the `vwap_wave` system. For each `(symbol, timeframe)` reachable through Alpaca, search a parameter grid on an in-sample (IS) window, validate on the next out-of-sample (OOS) window, walk forward through history, and emit a per-symbol live-config layer of best-known parameters — gated by the Pardo Walk-Forward Efficiency Ratio (WFE) and a positive aggregate OOS P&L floor.

The live engine must remain untouched apart from learning to read a per-symbol overrides file at boot.

### Requirements (from the operator brief)

- IS and OOS lengths configurable in months or days.
- Optimize timeframe alongside parameters.
- Scan all assets available at the broker.

### Non-goals (v1)

- Bayesian / random search (grid search only).
- Anchored windowing (rolling only; contract pinned by a `NotImplementedError` test).
- Walk-Forward Matrix (multi-`(IS, OOS)` sensitivity sweep) — design leaves room via `run_id` keying.
- Cross-setup interactions (one setup at a time per `(symbol, timeframe)` run).
- Distributed execution beyond a single host's joblib pool.
- Promotion of overrides into `settings.yaml` (operator decision, manual edit).

---

## 2. Architecture overview

```
                   ┌──────────────────────────────────────────────────────┐
                   │  scripts/run_wfo.py  (argparse CLI)                  │
                   │  loads settings.yaml + wfo.yaml, builds runner       │
                   └──────────────────────────────────────────────────────┘
                                            │
       ┌────────────────────────────────────┼────────────────────────────────────┐
       ▼                                    ▼                                    ▼
 universe.py                        windowing.py                              grid.py
 (broker scan,                      (rolling IS/OOS                  (per-setup grid expansion
  liquidity floor,                   splitter — months/days)         from declared YAML ranges)
  top-N filter)                            │                                    │
       │                                    ▼                                    ▼
       └─────► runner.py ◄───── fitness.py (Sharpe + min_trades floor) ◄────────┘
                  │
                  │   joblib.Parallel(n_jobs=-1)
                  ▼
       per-task: IntradayReplay(...).run()  →  BacktestResult
                  │
                  ▼
       results.parquet  (one row per (symbol, timeframe, walk, combo))
                  │
                  ▼
       report.py: aggregate per (symbol, timeframe), apply WFE+P&L gate
                  │
                  ▼
   runtime/wfo/<run_id>/   live_overrides.yaml  +  summary.md
                                            │
                                            ▼
                              main.py (live engine) reads
                              settings.yaml THEN layers
                              live_overrides.yaml on top
                              when present
```

WFO is an external orchestrator over the existing `IntradayReplay` engine — every parameter combo gets a fresh engine stack built from a synthesized config. No changes to `scheduler/loop.py` or `IntradayReplay`. The only live-engine touchpoint is `main.py` learning to layer overrides on top of `settings.yaml` at boot.

### Module layout

```
backtest/wfo/
├── __init__.py
├── windowing.py    # IS/OOS rolling splitter + "6mo"/"180d" parser
├── grid.py         # parameter-grid expansion (per-setup cartesian product)
├── fitness.py      # rank IS combos by Sharpe with min_trades floor
├── universe.py     # AlpacaClient assets scan + liquidity filter + top-N
├── runner.py       # orchestrator: walks × combos → IntradayReplay → results
└── report.py       # aggregate, apply WFE gate, emit overrides + summary

scripts/run_wfo.py  # CLI entry point

config/wfo.yaml     # WFO-specific config (universe, grids, IS/OOS, gate)
```

---

## 3. Components

### 3.1 `windowing.py`

Pure-function module. One public function and one helper:

```python
def parse_duration(s: str) -> relativedelta | timedelta
    # "6mo" → relativedelta(months=6); "180d" → timedelta(days=180); raises on bad input

def make_walks(start: datetime, end: datetime,
               in_sample: timedelta | relativedelta,
               out_of_sample: timedelta | relativedelta,
               step: timedelta | relativedelta | None = None) -> list[Walk]
```

`Walk` is a frozen dataclass with `(idx, is_start, is_end, oos_start, oos_end)`. `step` defaults to `out_of_sample` (classical rolling, non-overlapping OOS). Walks that don't fit a full IS+OOS at the end of history are dropped. An `anchored: bool` parameter is reserved by signature but raises `NotImplementedError` in v1.

### 3.2 `grid.py`

```python
@dataclass(frozen=True)
class ParamCombo:
    setup: str                    # "price_discovery" | "fade_extreme" | ...
    setup_values: dict[str, Any]  # {"atr_mult_stop": 1.0, "target_R": 1.5, ...}
    pm_values: dict[str, Any]     # {"max_hold_bars": 12, "breakeven_at_R": 1.0}
    fingerprint: str              # blake2b of sorted-canonical JSON

def expand_grid(grid_spec: dict, pm_spec: dict) -> list[ParamCombo]
```

Per setup: cartesian product over the lists declared under `wfo.grid.<setup>`. Each setup's grid is searched independently (one setup is "live" per run; others forced `enabled: False`). Position-management combos cross-multiply with each setup's grid.

### 3.3 `fitness.py`

```python
def score(metrics: dict, min_trades: int) -> float | None
    # returns metrics["sharpe"] if metrics["trades"] >= min_trades else None;
    # NaN Sharpe → None
```

Trivial today; isolated so a future MAR / expectancy / composite swap is one file.

### 3.4 `universe.py`

```python
def scan_alpaca_universe(
    client: AlpacaClient,
    *,
    classes: list[str],                    # ["us_equity", "crypto"]
    min_dollar_volume_20d: float,
    top_n_per_class: dict[str, int | None],
    cache_dir: str | Path,
    asof_date: date | None = None,
) -> list[tuple[str, str]]
```

Calls `GET /v2/assets`, filters `status == "active"` and `tradable == True` per `classes`, fetches the last 20 daily bars per candidate to compute `mean(close × volume)`, drops below the floor, sorts by liquidity desc, takes `top_n_per_class[class]` (None ⇒ no cap). Returns `(symbol, asset_class)` shape `IntradayReplay` already consumes. Cached on disk per `(asof_date, classes, floor, top_n)` to avoid re-scanning across runs.

### 3.5 `runner.py`

```python
@dataclass
class WFORunner:
    cfg: dict                                    # wfo.yaml + settings.yaml merged
    asset_class_configs: dict[str, AssetClassConfig]
    output_dir: Path                             # runtime/wfo/<run_id>/

    def run(self) -> Path                        # returns results.parquet path
```

Outer loops (serial, with progress bars): `(symbol, timeframe)`. Inner: `joblib.Parallel(n_jobs=cfg["run"]["parallelism"], backend="loky")` over `(walk, combo)`. Tasks are dispatched to a top-level pickle-friendly function `_run_one(task) -> ResultRow` (not a closure). Bars are fetched once per `(symbol, timeframe)` via existing `AlpacaData` parquet cache. Results stream into a `pyarrow.parquet.ParquetWriter` (append-friendly batches; default 1024 rows/flush) so a crash leaves a partial-but-valid file.

### 3.6 `report.py`

Loads `results.parquet`. Per `(symbol, timeframe, setup)` group:

1. For each walk pick `argmax(is_sharpe)` row (the IS-best combo for that walk).
2. Compute aggregate **WFE = Σ oos_sharpe / Σ is_sharpe** using only those per-walk winners. When `Σ is_sharpe ≤ 0` mark `wfe = NaN` and fail the gate.
3. Compute aggregate `total_oos_pnl = Σ oos_pnl`.
4. **Gate**: pass iff `wfe ≥ cfg.gate.wfe_min` (default 0.5) AND (`total_oos_pnl > 0` if `require_positive_oos_pnl` else true).
5. Per-symbol selection: among `(timeframe, setup)` candidates that passed, pick the one with highest aggregate OOS Sharpe; tie-break deterministically by sorted `(timeframe, setup)`.
6. Live params come from the **last walk's IS-best combo** for the selected `(symbol, timeframe, setup)` — freshest data wins.

Emits `live_overrides.yaml` and `summary.md`.

---

## 4. Config schema

WFO config lives in `config/wfo.yaml` (separate from `settings.yaml`). The CLI loads both, merges, and never mutates `settings.yaml`.

```yaml
# config/wfo.yaml — Walk-Forward Optimization meta-config

run:
  output_root: runtime/wfo               # <run_root>/<run_id>/...
  random_seed: 42                        # for any randomized fitness ties
  parallelism: -1                        # joblib n_jobs; -1 = all cores

history:
  start: "2024-01-01"
  end: "2026-04-30"                      # inclusive end; UTC
  initial_equity: 100_000

windowing:
  in_sample: "6mo"                       # parser: <int>(d|mo)
  out_of_sample: "1mo"
  step: null                             # null → step == out_of_sample

universe:
  source: alpaca_scan                    # alpaca_scan | symbols
  symbols: []                            # used iff source: symbols
  alpaca_scan:
    classes: [us_equity, crypto]
    min_dollar_volume_20d: 5_000_000
    top_n_per_class:
      us_equity: 100
      crypto: null                       # null = no cap (all crypto)
    cache_dir: runtime/wfo/universe_cache

timeframes:                              # joint with the param grid
  - 5Min
  - 15Min
  - 30Min
  - 1Hour

fitness:
  metric: sharpe                         # only "sharpe" wired in v1
  min_trades: 20                         # combos below this floor are dropped

gate:
  wfe_min: 0.5                           # Pardo's WFE acceptance floor
  require_positive_oos_pnl: true

grid:                                    # per-setup parameter ranges
  price_discovery:
    enabled: [true]
    atr_mult_stop:    [0.75, 1.0, 1.25, 1.5]
    target_R:         [1.0, 1.5, 2.0, 2.5]
    arm_window_bars:  [4, 6, 8]
    cooldown_bars:    [12]
  fade_extreme:
    enabled: [true]
    atr_mult_stop:    [0.5, 0.75, 1.0]
    scale_offsets_atr: [[0.0, 0.25, 0.5]]      # fixed for v1 (tuned together)
    scale_weights:     [[0.4, 0.35, 0.25]]
    cooldown_bars:    [12]
  return_to_value:
    enabled: [true]
    atr_mult_stop:    [0.75, 1.0, 1.25]
    arm_window_bars:  [4, 6, 8]
    cooldown_bars:    [12]
  vwap_bounce:
    enabled: [true]
    atr_mult_stop:    [1.0, 1.25, 1.5]
    target_R:         [1.5, 2.0, 2.5]
    arm_window_bars:  [4, 6]
    cooldown_bars:    [8]

position_management:                     # cross-multiplied with each setup grid
  max_hold_bars:     [8, 12, 16]
  breakeven_at_R:    [0.75, 1.0, 1.25]
```

Notes on shape:

- **`source: alpaca_scan` vs `symbols`** — switches `universe.py` between broker scan and an explicit list. No CLI flag soup; one mode declared in YAML.
- **List-of-lists** for `scale_offsets_atr` / `scale_weights` — each top-level entry is *one* candidate value (a list itself); v1 keeps these fixed because they're tuned together.
- **Per-setup independence in v1** — each setup's grid is searched independently within a `(symbol, timeframe)` — `Σ |grid_setup_i|` combos per `(symbol, timeframe, walk)`, not the full cartesian product across setups. Cross-setup interactions are a v2 study (§9).
- **`gate.wfe_min: 0.5`** matches Pardo's literature floor; `require_positive_oos_pnl: true` is the second guardrail.

A new key in `settings.yaml` wires the live engine to the override file (see §6.4):

```yaml
overrides:
  path: runtime/wfo/latest/live_overrides.yaml
  enabled: true     # set false to ignore overrides without renaming the file
```

`runtime/wfo/latest` is a symlink the CLI updates on success — the live engine always reads the latest passing run.

---

## 5. Algorithm

### 5.1 Boot (`scripts/run_wfo.py`)

1. Parse `--config config/wfo.yaml` and `--settings config/settings.yaml`.
2. Generate `run_id = <UTC-ISO-min>_<short-hash-of-merged-config>` (deterministic for re-runs ⇒ enables resume).
3. Create `runtime/wfo/<run_id>/`; write `manifest.json` immediately (merged config + git SHA + start time).
4. Build the `(symbol, asset_class)` universe via `universe.scan_alpaca_universe(...)`; persist to `universe.parquet`.
5. Build walks via `windowing.make_walks(history.start, history.end, in_sample, out_of_sample)`.
6. Build per-setup combo lists via `grid.expand_grid(cfg["grid"], cfg["position_management"])`.
7. Hand the runner the materialized lists.

### 5.2 Outer loop — `(symbol, timeframe)`

Serial. For each pair:

a. **Load bars once** via `AlpacaData.get_bars(symbol, asset_class, timeframe, start=history.start, end=history.end)`. Fail soft per pair (log `BARS_UNAVAILABLE symbol=X tf=Y`, continue).

b. **Slice once.** For each walk, build `walk.is_bars` and `walk.oos_bars` by filtering on `bar.ts`. Stored once in the parent process; child workers inherit via copy-on-write fork.

c. **Build joblib tasks** as `walks × all_combos`. **Resume:** drop tasks whose `(symbol, timeframe, walk_idx, combo.fingerprint)` key already exists in `results.parquet`.

d. **Dispatch.** `joblib.Parallel(n_jobs=cfg["run"]["parallelism"], backend="loky")(delayed(_run_one)(task) for task in tasks)`.

### 5.3 Per-task — `_run_one(task) -> ResultRow`

For one `(symbol, timeframe, walk, combo)`:

1. Build a one-symbol **IS config dict** by overlaying `combo.setup_values` and `combo.pm_values` onto a `wfo_settings_template` (a synthesized version of `settings.yaml` with this combo's setup params). All other setups are forced `enabled: False` for this run (clean attribution).
2. Run **IS leg**:
   ```python
   IntradayReplay(
       symbols=[(symbol, ac)],
       asset_class_configs=...,
       bars={symbol: walk.is_bars},
       initial_equity=cfg["history"]["initial_equity"],
       config=is_config,
   ).run()
   ```
   → `is_metrics = compute_metrics(is_result.equity_curve, is_result.trades)`.
3. **Fitness floor**: if `is_metrics["trades"] < cfg.fitness.min_trades` write the row with `is_score=NaN, oos_*=NaN, status="below_min_trades"` and return — skip OOS.
4. Run **OOS leg**: same config, `bars={symbol: walk.oos_bars}` → `oos_metrics`.
5. Return a `ResultRow` (single dict) with:
   ```
   symbol, asset_class, timeframe, walk_idx,
   setup, fingerprint, combo_values_json,
   is_sharpe, is_trades, is_pnl,
   oos_sharpe, oos_trades, oos_pnl, oos_max_dd, oos_avg_R,
   status, error
   ```

Rows stream back to the parent and are appended to the `ParquetWriter`. No in-memory accumulation of millions of rows.

### 5.4 Aggregate + gate (`report.py`)

After the runner finishes — see §3.6 selection rules. Aggregate metrics (mean OOS Sharpe, WFE, total OOS P&L, walks evaluated) are emitted per `(symbol, timeframe, setup)` row to `summary.md`.

### 5.5 Emit artifacts

Under `runtime/wfo/<run_id>/`:

- `results.parquet` — one row per `(symbol, timeframe, walk, combo)`; written incrementally during 4.3.
- `live_overrides.yaml` — per-symbol best `(timeframe, setup, params)`; only symbols that passed the gate.
- `summary.md` — ranked table per asset class.
- `manifest.json` — updated with end time and gate stats (`evaluated`, `passed`, `failed`, mean WFE among passing).
- `errors.log` — JSON-line per failed task.
- `run.log` — Python `logging` output at INFO+ for the whole run.

The CLI atomically updates the `runtime/wfo/latest` symlink to point at this run dir on **success** (gate-pass count > 0).

---

## 6. Live-config layering

### 6.1 `live_overrides.yaml` shape

```yaml
# Generated by scripts/run_wfo.py
# run_id: 2026-05-19T14-23_a3f1c2
# git_sha: <sha>
# wfe_min: 0.5  require_positive_oos_pnl: true
# gate-passed: 14 / 23 evaluated symbols

symbols:
  AAPL:
    timeframe: 15Min
    setup: price_discovery
    setup_params:
      atr_mult_stop: 1.25
      target_R: 2.0
      arm_window_bars: 6
    position_management:
      max_hold_bars: 12
      breakeven_at_R: 1.0
    metadata:
      walks: 30
      mean_oos_sharpe: 1.42
      wfe: 0.78
      total_oos_pnl: 4213.50
```

Symbols absent from this file get **no override** — they fall through to canonical `settings.yaml`. Symbols present override only the listed keys; everything else (filters, risk, scheduler cadence, asset-class config) still comes from `settings.yaml`.

### 6.2 `main.py` layering

A new helper `apply_overrides(cfg, overrides_path) -> cfg` runs once after `load_config`. Two consumption points downstream:

1. **`build_setups(cfg, symbol)`** — if `symbol in cfg["_per_symbol_overrides"]`, build *only* the overridden setup with its overridden params; ignore the global `setups.*` block for that symbol. Other symbols keep global behaviour.
2. **`PositionManager` construction** — promote to a small `position_manager_for(symbol, cfg)` factory returning a per-symbol `PositionManager` when overrides exist, and a shared one otherwise. `VWAPWaveEngine.tick`'s `pm.on_bar(symbol, bar)` contract is unchanged.

### 6.3 Per-symbol timeframe — the one engine subtlety

The scheduler currently drives the bar-clock at a single `cfg["scheduler"]["bar_timeframe"]`. With per-symbol overrides each `(symbol, timeframe)` may differ.

**Resolution:** the scheduler ticks at the **finest** timeframe across all configured symbols (e.g., `min(5Min, 15Min, 30Min) = 5Min`). For each symbol, on each tick we fetch its native timeframe and only pass bars whose `ts` is a fresh boundary for *that* symbol — 15Min symbols see fresh bars every 3rd 5Min tick. `VWAPWaveEngine.tick`'s contract `fresh_bars: dict[symbol, list[Bar]]` is unchanged; symbols simply have empty-bar ticks until their boundary lands.

If no overrides are loaded, behaviour reduces exactly to today's single-timeframe loop.

### 6.4 `settings.yaml` addition

```yaml
overrides:
  path: runtime/wfo/latest/live_overrides.yaml
  enabled: true     # set false to ignore overrides without renaming the file
```

---

## 7. Errors, resumability, observability

### 7.1 Failure model

**Per-task failure.** `_run_one` wraps its body in a top-level `try/except`. On exception it returns a `ResultRow` with `status="failed", error=repr(exc)`, all metric columns `NaN`. The runner counts but never re-raises. The aggregator in `report.py` ignores `status != "ok"` rows when picking IS winners. One JSON-line per failure goes to `errors.log`.

**Per-`(symbol, timeframe)` failure.** Bar fetch failure or empty bars: logged as `BARS_UNAVAILABLE`/`ALL_WALKS_FAILED`; the runner moves to the next pair. Skipped pairs contribute zero rows to `results.parquet` and show as "no data" in `summary.md`.

**Run-level failure.** Out-of-disk, KeyboardInterrupt, host reboot — handled by resumability (§7.2).

### 7.2 Resumability

Single durability invariant: **`results.parquet` is the source of truth for what's done.**

- Runner uses a `pyarrow.parquet.ParquetWriter` opened in append-friendly mode (one batch per N completed tasks, default 1024). A kill leaves a valid parquet with whatever batches were flushed.
- On startup, if `manifest.json` already exists in the target run dir (a resume of an existing `run_id`):
  1. Load existing rows from `results.parquet` into a set keyed by `(symbol, timeframe, walk_idx, fingerprint)`.
  2. Generate the full task list as usual.
  3. Filter: drop tasks whose key is already present.
  4. Run only the remainder.
- Resume happens automatically when the operator passes the same `--config` (since `run_id` is a deterministic hash of the merged config). To force a fresh run, the operator passes `--run-id <new>` or deletes the directory.

### 7.3 Atomic artifact swap

`live_overrides.yaml` and `summary.md` are written via `tmp + os.replace` (same pattern as `state/dashboard_state.py`). The `runtime/wfo/latest` symlink swap also goes through `tmp + os.replace` on the symlink — no window where the live engine reads a half-written file.

If the gate passes for **zero** symbols, the runner still writes `live_overrides.yaml` (with `symbols: {}`) and **does not** update `runtime/wfo/latest`. The previous `latest` keeps pointing to the last passing run; live behaviour is unchanged. `summary.md` records the empty result.

### 7.4 Observability

Three layers:

1. **Console** — single `tqdm` progress bar for outer `(symbol, timeframe)` pairs, with summary postfix (`completed=K/N pass=P fail=F`). Inner per-task progress is suppressed by default; `--verbose` enables joblib `verbose=10`.
2. **Structured run log** — `runtime/wfo/<run_id>/run.log` captures Python `logging` at INFO+. Boot manifest, per-pair start/finish with duration and pass/fail counts, all `ERROR` events from task failures, end-of-run gate summary.
3. **Inline metrics in `summary.md`** — top: run config snapshot (IS/OOS, universe size, total tasks, wall-clock). Middle: ranked table. Bottom: gate stats (`evaluated=N pass=K fail=N-K`, mean WFE among passing).

No time-series metrics in v1; `run.log` is structured enough for downstream ingestion.

### 7.5 Cancel UX

`Ctrl-C` during a run: joblib propagates `KeyboardInterrupt` to workers; runner catches at top, flushes the `ParquetWriter`, writes `manifest.json` with `status: "interrupted"`. Re-running with the same config picks up where it stopped. The `latest` symlink is never updated by an interrupted run.

---

## 8. Testing strategy

Suite remains <30 s. Seven layers, each in its own test file:

### 8.1 `tests/test_wfo_windowing.py`

- `parse_duration("6mo")` and `"180d"` produce expected types; bad strings raise.
- `make_walks` over a fixed `(start, end, IS=180d, OOS=30d)` produces the expected count and contiguous OOS coverage.
- `step=15d` on a 30d OOS yields overlapping OOS walks.
- IS+OOS not fitting at end of history → walk dropped (no partial-IS walks).
- `anchored=True` raises `NotImplementedError`.

### 8.2 `tests/test_wfo_grid.py` + `tests/test_wfo_fitness.py`

- A 2-key grid `[a,b] × [c,d]` expands to 4 combos with stable fingerprints; same input twice → same fingerprints.
- Reordered YAML lists produce the same fingerprints (sorted-canonical hashing contract).
- Position-management combos cross-multiply with setup grids correctly.
- `score(metrics, min_trades=20)` returns Sharpe when `trades >= 20`, `None` when below floor; NaN Sharpe → `None`.

### 8.3 `tests/test_wfo_run_one.py`

- Tiny `(symbol, asset_class, timeframe, walk, combo)` against a synthetic flat-bars fixture; `ResultRow` schema is correct (every column present, types right, `status="ok"`).
- Bars producing zero IS trades → `is_score=NaN, oos_*=NaN, status="below_min_trades"`.
- Combo that triggers a deliberate exception inside `IntradayReplay` (monkeypatched) → row has `status="failed"`, `error` set, **runner does not re-raise**; outer two of three tasks complete.
- Idempotency: same input twice → byte-identical `ResultRow` (extends Phase 8's determinism gate).

### 8.4 `tests/test_wfo_runner.py`

- Smoke: 1 symbol × 1 timeframe × 2 walks × 4 combos against synthetic bars completes; `results.parquet` has the expected row count.
- **Resumability**: write a `results.parquet` with half the expected rows, run again with same config — final file has exactly the missing rows added (no duplicates), original rows untouched.
- Per-pair failure isolation: `(symbol_A, tf)` returns bars, `(symbol_B, tf)` returns `[]` (mocked) — runner completes, `symbol_A` rows present, `symbol_B` absent and logged.

### 8.5 `tests/test_wfo_report.py`

- Hand-built `results.parquet` with known IS/OOS Sharpe per walk → per-walk argmax selects the right combos and aggregate WFE matches the closed-form value.
- Gate pass: WFE=0.7, OOS PnL>0 → emitted. Gate fail: WFE=0.3 → omitted; symbol absent.
- Edge: `Σ IS Sharpe ≤ 0` → `WFE=NaN`, gate fails.
- Multiple `(timeframe, setup)` candidates per symbol all pass → highest aggregate OOS Sharpe wins; tie-broken deterministically by sorted `(timeframe, setup)`.
- Empty pass set → `live_overrides.yaml` written with `symbols: {}`; `latest` symlink **not** updated (use a `--dry-symlink` seam).

### 8.6 `tests/test_main_overrides.py`

- `apply_overrides(cfg, overrides_path=None)` returns `cfg` unchanged.
- With overrides present, `build_setups(cfg, "AAPL")` returns only the overridden setup with overridden params; other setups suppressed for that symbol.
- `position_manager_for(symbol, cfg)` returns a per-symbol PM with override values when present; the shared global PM otherwise.
- Boot smoke: existing `TRADING_ENV=test ... python -c "import main; print('ok')"` plus a new variant with a fixture overrides file present → still ok.

### 8.7 `tests/test_wfo_universe.py`

- Mock `AlpacaClient.get_assets` returning a fixed list with mixed `tradable`/`status`; mock daily bar calls with synthetic volume — assert `scan_alpaca_universe` filters, sorts by liquidity, applies `top_n_per_class`, and returns `(symbol, asset_class)` shape.
- Cache hit: second call with same parameters reads from `runtime/wfo/universe_cache/...parquet` and **does not** call the client.

### Out of scope for the test suite

- Full WFO CLI end-to-end against real Alpaca data (manual smoke).
- Performance / wall-clock benchmarks.
- joblib parallelism semantics (trust the library; tests run with `n_jobs=1` via the `parallelism` config knob).

---

## 9. Open questions (deferred to v2)

- **Anchored windowing** — the `Walk` shape and `make_walks` signature already accommodate it; a v2 PR can lift the `NotImplementedError` and flip the semantics behind `anchored: bool`.
- **Walk-Forward Matrix** — running multiple `(IS, OOS)` pairs in one campaign and emitting a sensitivity matrix. `run_id` keying already supports this — the next iteration adds an outer loop and a different report shape.
- **Cross-setup interaction** — currently each setup is searched independently. A future study can search the cartesian product across all four setups, paying the compute cost.
- **Promotion workflow** — currently `live_overrides.yaml` is read-only by the live engine; a future tool could `wfo promote --run-id X` to merge into `settings.yaml` after operator review.

---

## 10. References

- AlgoTrading101, *Walk-Forward Optimization* — risk-adjusted fitness over raw P&L; consistency check via 10 WFO variants. <https://algotrading101.com/learn/walk-forward-optimization/>
- Wikipedia, *Walk Forward Optimization* — algorithm summary, IS/OOS rolling structure.
- Pardo, *The Evaluation and Optimization of Trading Strategies* (2nd ed., 2008) — pp. 263–300; canonical Walk-Forward Efficiency Ratio definition.
- StrategyQuant, *Walk-Forward Optimization* — terminology for Walk-Forward Matrix; default fitness conventions (URL was 403 at design-time; consulted via prior knowledge).
