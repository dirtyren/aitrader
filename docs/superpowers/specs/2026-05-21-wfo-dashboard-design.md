# WFO Dashboard — Design

**Status:** draft for review
**Author:** brainstormed in-session
**Date:** 2026-05-21
**Targets:** main branch (post the existing WFO design at `2026-05-19-walk-forward-optimization-design.md`)

---

## 1. Goal

Add a Streamlit-driven workflow on top of the existing Walk-Forward Optimization (WFO) engine so an operator can, from the dashboard:

1. **Launch** WFO runs with an interactive form covering universe (top-N per asset class), windowing, and gate.
2. **Browse** past and current runs, with live progress for the running job and a queue for pending ones.
3. **Visualize** results per `(symbol, timeframe, setup)` with a comparison table and four chart types: walk-by-walk OOS equity curve, per-walk OOS Sharpe bars, IS-vs-OOS Sharpe scatter, and a parameter heatmap.
4. **Approve** the new per-symbol parameters into a single active overrides file that the live trader reads on its next restart — replacing the existing `runtime/wfo/latest` auto-promote symlink with an explicit candidate → active two-stage flow.

The WFO engine itself (`backtest/wfo/*`) is unchanged. The CLI entry point (`scripts/run_wfo.py`) loses one line — its symlink update — and gains nothing else. All new surface lives in `ui/wfo/` plus minimal edits to `main.py`, `config/settings.yaml`, and `docker-compose.yml`.

### Non-goals (v1)

- Hot-reload of overrides into a running trader. Approval takes effect on the next trader restart.
- Bar-level OOS equity curves. Curves are rendered from per-walk `oos_pnl` (one point per walk).
- Editing the WFO param grids or position-management cross-product from the form. Those remain in `config/wfo.yaml`.
- Multi-operator auth on the dashboard. It remains single-user, single-host.
- Approving candidates before a run completes. Approve only on `status="completed"` runs.
- Distributed / cross-host job execution.
- Per-run retention or cleanup policy.

---

## 2. Architecture overview

A new **WFO** tab in `ui/dashboard.py`, a small in-container **job supervisor** thread that runs the existing `scripts.run_wfo` CLI as serialized subprocesses, and a contract change that splits the existing `runtime/wfo/latest` symlink into two artifacts: per-run **candidate** files and a single dashboard-managed **active** file.

### 2.1 Container topology

```
┌─────────── dashboard container ────────────┐    ┌── trader container ──┐
│                                              │    │                       │
│   streamlit (ui/dashboard.py)               │    │   main.py             │
│   ├── Overview tab (existing)                │    │   reads on boot:      │
│   ├── Logs tab (existing)                    │    │     runtime/wfo/      │
│   └── WFO tab (NEW)                          │    │       active/         │
│         ├── Runs list                        │    │       live_overrides  │
│         ├── New Run form                     │    │       .yaml           │
│         ├── Run Detail / charts              │    │                       │
│         └── Active Overrides panel           │    │                       │
│                                              │    │                       │
│   ui/wfo/supervisor.py (NEW)                 │    │                       │
│   ├── Polls runtime/wfo/jobs/queue/          │    │                       │
│   ├── Spawns `python -m scripts.run_wfo`     │    │                       │
│   ├── Tracks one active subprocess at a time │    │                       │
│   └── Updates runtime/wfo/jobs/<job>.json    │    │                       │
└──────────────────────────────────────────────┘    └───────────────────────┘

         shared volume: ./runtime → /app/runtime
         shared volume: ./logs    → /app/logs
```

### 2.2 New surface

- **`ui/wfo/`** — package: `tab.py`, `forms.py`, `runs_list.py`, `run_detail.py`, `charts.py`, `approval.py`, `supervisor.py`, `job_state.py`.
- **`runtime/wfo/jobs/`** — orchestration directory: `queue/`, `active/`, `history/`, `configs/`, plus `supervisor.lock`.
- **`runtime/wfo/active/live_overrides.yaml`** — new dashboard-managed file. Replaces `runtime/wfo/latest/live_overrides.yaml` as the live engine's source.
- **`runtime/wfo/active/audit.jsonl`** — append-only approval ledger.
- **`runtime/wfo/presets/`** — saved form payloads (quality-of-life, not load-bearing).

### 2.3 Unchanged

- `backtest/wfo/*` (engine internals: windowing, grid, fitness, runner, report functions).
- `scripts/run_wfo.py` semantics other than removing the `update_latest_symlink_if_passing(...)` call.
- `main.py` apart from the override file path it reads.
- Existing dashboard tabs (Overview, Logs).
- The existing 178-test suite.

---

## 3. Job state, lifecycle, queue, cancel

Three concerns are decoupled: how a run is **requested** (UI form → queue file), how the **supervisor** serializes work, and how the UI **observes** progress.

### 3.1 File layout

```
runtime/wfo/jobs/
├── queue/
│   ├── 2026-05-21T14-02-11_a3f1.json     # queued, oldest first by filename
│   └── 2026-05-21T14-05-30_b8c2.json
├── active/
│   └── 2026-05-21T13-58-44_77ee.json     # 0 or 1 file — the running job
├── history/
│   └── 2026-05-21T13-30-00_aabb.json     # completed/cancelled/failed
├── configs/
│   └── 2026-05-21T14-02-11_a3f1.yaml     # frozen wfo.yaml passed to the CLI
└── supervisor.lock                       # flock'd by the running supervisor
```

`runtime/wfo/<run_id>/` (engine artifact dir) is unchanged. `jobs/` is a thin orchestration layer **outside** of `<run_id>/` so artifact dirs stay self-contained and engine code stays unaware of the dashboard.

### 3.2 Job state file

One JSON file per job, moved across `queue/` → `active/` → `history/` directories on each transition (rename within the same volume is atomic):

```json
{
  "job_id": "2026-05-21T14-02-11_a3f1",
  "run_id": "2026-05-21T14-02-11_a3f1c2",
  "status": "queued|starting|running|completed|cancelled|failed",
  "queued_at": "...", "started_at": "...", "completed_at": "...",
  "form_payload": { "universe": {...}, "windowing": {...}, "gate": {...} },
  "wfo_config_path": "runtime/wfo/jobs/configs/<job_id>.yaml",
  "pid": 12345,
  "exit_code": 0,
  "error": null,
  "progress": {
    "total_pairs": 400, "completed_pairs": 137,
    "current_symbol": "AAPL", "current_timeframe": "15Min",
    "rows_written": 41280, "elapsed_s": 312.4, "eta_s": 588.0
  }
}
```

Status transitions: `queued → starting → running → (completed | cancelled | failed)`.

### 3.3 Supervisor (`ui/wfo/supervisor.py`)

A small loop running **inside the dashboard container** but **not** inside Streamlit's request handlers:

- Started lazily on dashboard boot via a single-flight thread, guarded by `supervisor.lock` flock so multiple Streamlit reruns/sessions don't spawn multiple supervisors.
- Loop: pick the oldest file in `queue/`, move it to `active/`, write its frozen `runtime/wfo/jobs/configs/<job_id>.yaml` (form payload merged into the canonical `config/wfo.yaml` template), spawn `python -m scripts.run_wfo --config <that file>` as a subprocess, wait for exit.
- While the child runs: every ~2 s, supervisor reads `runtime/wfo/<run_id>/manifest.json` plus a cheap row count on `results.parquet`, updates `progress.*` in the active job file.
- On exit: capture exit code, set `completed_at`, move file to `history/<job_id>.json`. Loop continues with the next queued file.

**Why a thread inside Streamlit and not a separate `supervisor` service?** Per the architectural choice in §2 (subprocess inside the dashboard container), a singleton thread guarded by flock is the lightest thing that works in one container. Promoting it to a separate compose service if/when needed is a 1-day change.

### 3.4 UI observations

- **Runs list** reads `jobs/queue/`, `jobs/active/`, `jobs/history/` (cheap `glob` + JSON parse). No DB.
- **Live progress** reads `jobs/active/<job_id>.json` directly; the dashboard's existing 5 s autorefresh suffices.
- **Cancel** writes a sentinel file `jobs/active/<job_id>.cancel`. Supervisor checks for it on each progress tick; if present, sends `SIGTERM` to the child, waits up to 10 s for graceful exit (the WFO runner already handles `KeyboardInterrupt` by flushing the parquet writer and writing `manifest.status="interrupted"`), then `SIGKILL` if needed. Final status: `cancelled`.
- **Queue** is just files in `jobs/queue/`. UI lets you remove a queued entry (delete the file) only while `status=queued`. Reordering is supported via filename (rename for new sort key); editing a queued payload is **not** supported — to change params, remove and re-create.

### 3.5 Concurrency invariants

- **One active job at a time** — enforced by the supervisor (only one file in `jobs/active/` at a time; next picked from `queue/` after the previous exits).
- **One supervisor per dashboard container** — `supervisor.lock` flock at boot.
- **Orphan handling** — if the dashboard restarts mid-run, the next boot sees a job in `jobs/active/` whose PID is dead. The supervisor marks it `failed` with `error="dashboard restart, orphaned"`, moves it to `history/`, and proceeds with `queue/`. The associated `runtime/wfo/<run_id>/` directory is preserved; the user can re-launch with the same form values to resume (the engine's deterministic `run_id` already supports resume).

---

## 4. Candidate → active two-stage flow

The contract change. Today: WFO CLI auto-promotes via `runtime/wfo/latest` symlink, live engine reads it on boot. New: WFO CLI just writes a candidate; the dashboard promotes per symbol into a single active file.

### 4.1 File contract

```
runtime/wfo/<run_id>/live_overrides.yaml      # CANDIDATE — written by run_wfo.py
                                              # one file per run, immutable
runtime/wfo/active/live_overrides.yaml        # ACTIVE — read by main.py at boot
                                              # mutated only by dashboard approvals
runtime/wfo/active/audit.jsonl                # NEW — append-only approval ledger
```

The `runtime/wfo/latest` symlink is no longer written. Symbols absent from `active/live_overrides.yaml` fall through to `config/settings.yaml`, exactly as today.

### 4.2 Active file shape

Same schema as candidate, plus a `_provenance` block per symbol recorded by the approval action:

```yaml
symbols:
  AAPL:
    timeframe: 15Min
    setup: price_discovery
    setup_params: { atr_mult_stop: 1.25, target_R: 2.0, arm_window_bars: 6 }
    position_management: { max_hold_bars: 12, breakeven_at_R: 1.0 }
    metadata: { walks: 30, mean_oos_sharpe: 1.42, wfe: 0.78, total_oos_pnl: 4213.50 }
    _provenance:
      run_id: 2026-05-21T14-02_a3f1c2
      approved_at: 2026-05-21T15:11:08Z
      approved_by: dashboard
```

Provenance is informational; `main.py`'s `apply_overrides` ignores keys starting with `_`.

### 4.3 Approval action (per-symbol)

Triggered by an "Approve" button on a row in Run Detail. Atomic, audit-logged steps:

1. Load `runtime/wfo/active/live_overrides.yaml` (or `{symbols: {}}` if missing).
2. Replace the entry under `symbols.<SYM>` with the candidate run's entry for that symbol, plus a freshly stamped `_provenance`.
3. Write to `runtime/wfo/active/live_overrides.yaml.tmp`, then `os.replace` → `live_overrides.yaml`.
4. Append a JSON line to `runtime/wfo/active/audit.jsonl`:
   ```json
   {"ts":"...","action":"approve","symbol":"AAPL","run_id":"...","prev_run_id":"...","operator":"dashboard"}
   ```

**Approve is gate-gated.** Rows whose candidate failed the gate (WFE below threshold or OOS PnL non-positive when required) have no Approve button; server-side validation also rejects approval attempts from stale UI state.

### 4.4 Reject and Revert

- **Reject** on a candidate row: pure UI session state — marks the row "rejected" for the current view. **Non-sticky**: re-opening the run shows all rows as undecided.
- **Revert** on an active symbol: removes the entry from `active/live_overrides.yaml` (atomic write, audit-logged). Symbol falls back to `config/settings.yaml` defaults. To restore an old set, re-approve from the originating run dir.

### 4.5 First deploy

`runtime/wfo/active/live_overrides.yaml` won't exist on first deploy. `main.py` reads missing-file as `{symbols: {}}` (no overrides) — the live engine falls through to `settings.yaml` for every symbol. The old `runtime/wfo/latest/live_overrides.yaml`, if present, is **not** auto-migrated; the operator approves explicitly per symbol from the originating run.

---

## 5. UI surface — the WFO tab

One Streamlit tab added to `ui/dashboard.py` via `st.tabs(["Overview", "Logs", "WFO"])`. Internal state held in `st.session_state` (selected panel, selected run_id). No URL routing.

### 5.1 Layout

```
┌─ WFO tab ───────────────────────────────────────────────────────────────┐
│  [ Runs ]  [ New Run ]  [ Active Overrides ]                            │
│  ─────────────────────────                                               │
│                                                                          │
│  ┌─ panel content (one of four) ─────────────────────────────────────┐  │
│  │   ▸ Runs list           (default; entry to Run Detail)             │  │
│  │   ▸ Run Detail          (drilled in from Runs list)                │  │
│  │   ▸ New Run             (form + launch)                            │  │
│  │   ▸ Active Overrides    (current per-symbol live params)           │  │
│  └────────────────────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────────────────┘
```

### 5.2 Runs list

Top: **Active job banner** if `jobs/active/` is non-empty — `(symbol N/M)`, elapsed/ETA, progress bar, `[Cancel]`. Below: **Queued jobs** (oldest-first, `[Remove]` per row).

Main table — one row per directory in `runtime/wfo/<run_id>/`, sorted by completion time desc:

| run_id | started | finished | duration | universe size | walks × combos | gate pass | status | actions |
|--------|---------|----------|----------|----------------|----------------|-----------|--------|---------|
| 2026-05-21T14-02_a3f1 | 14:02 | 14:38 | 36m | 187 | 30 × 432 | 14 / 23 | completed | [Open] |
| 2026-05-20T19-05_77ee | 19:05 | — | 12m | 187 | — | — | cancelled | [Open] |

Source: each run's `manifest.json` plus a cheap parquet row count.

### 5.3 Run Detail

1. **Header**: run_id, git_sha, status, gate config used, total tasks evaluated/passed/failed, run duration, [Back to Runs].
2. **Per-symbol summary table** — one row per `(symbol, timeframe, setup)` group (the comparison table from §4.3):

   | Symbol | Currently active | Candidate (this run) | Δ / actions |
   |--------|--------------------|------------------------|-------------|
   | AAPL | 15Min · price_discovery · stop=1.0, R=1.5, … (run X) | 15Min · price_discovery · stop=1.25, R=2.0, … (this run) | diff badges + [Approve] [Reject] |
   | TSLA | *(none — using settings.yaml)* | gate **failed** (WFE=0.32) | (no approve button) |
   | NVDA | 30Min · vwap_bounce (run Y) | 5Min · price_discovery (this run) | timeframe-change warning + [Approve] |

   Sortable by mean_oos_sharpe / wfe / total_oos_pnl. Filters: asset class, gate-pass-only, "differs from active". Per-row actions and a row-expand for charts.

3. **Per-symbol charts** (lazy-rendered on row expand, Plotly via `st.plotly_chart`):
   - Walk-by-walk **OOS equity curve** (line, one point per walk) + per-walk **OOS Sharpe bars**, stacked.
   - **IS-vs-OOS Sharpe scatter** (per walk, with y=x reference line — overfit visual cue).
   - **Param heatmap** over the two most-impactful params for that setup (deterministic per setup type — e.g., `atr_mult_stop × target_R` for `price_discovery`, `atr_mult_stop × max_hold_bars` for `fade_extreme`, `atr_mult_stop × arm_window_bars` for `return_to_value`, `atr_mult_stop × target_R` for `vwap_bounce`).

4. **Run-level summary** — `summary.md` rendered at the bottom for context (existing artifact embedded inline).

Charts read from `runtime/wfo/<run_id>/results.parquet`. The OOS equity curve uses each row's `oos_pnl` per walk; bar-level curves are out of scope (would require an engine extension).

### 5.4 New Run form

Single-column form covering universe + windowing + gate. Per the constraint in §1, param grids and position-management cross-product remain in `config/wfo.yaml`; the form payload merges into that template at queue-write time.

```
┌─ Universe ──────────────────────────────────────────────┐
│  Source: ( ) Top-N scan   ( ) Explicit symbols          │
│                                                          │
│  ▸ Top-N scan:                                          │
│     Asset classes: [x] us_equity  [x] crypto            │
│     Top-N per class:                                    │
│       us_equity: [ 100 ]   crypto: [ blank=all ]        │
│     Min 20d $ volume: [ 5_000_000 ]                     │
│     Allowlist (force-include): [ multiselect ]          │
│     Blocklist (exclude):       [ multiselect ]          │
│     [ Preview universe ]                                │
└──────────────────────────────────────────────────────────┘
┌─ Windowing ─────────────────────────────────────────────┐
│  IS length:  [ 6mo ]    OOS length: [ 1mo ]             │
│  Step:       [ blank = OOS length ]                     │
│  History start: [ 2024-01-01 ]   end: [ 2026-04-30 ]    │
│  Timeframes: [x] 5Min [x] 15Min [x] 30Min [x] 1Hour     │
└──────────────────────────────────────────────────────────┘
┌─ Gate ──────────────────────────────────────────────────┐
│  WFE min:                  [ 0.5 ]                      │
│  Require positive OOS PnL: [x]                          │
│  Min trades floor (IS):    [ 20 ]                       │
└──────────────────────────────────────────────────────────┘
┌─ Estimate ──────────────────────────────────────────────┐
│  Universe: 187 symbols × 4 timeframes × 30 walks × 432  │
│  combos = ~9.7M tasks  (~est. 6h on 8 cores)            │
│  ⚠ High — consider reducing combos or universe.         │
└──────────────────────────────────────────────────────────┘

         [ Launch Run ]    [ Save as preset ]
```

- **Preview universe** calls `scan_alpaca_universe(...)` synchronously from the dashboard process (possible because §6.1 wires `config/.env` into the dashboard service). Cached per `(asof_date, classes, floor, top_n)` via the existing `runtime/wfo/universe_cache/` path the CLI uses.
- **Estimate panel** is an order-of-magnitude hint, not a precise wall-clock prediction. Computed from universe size × selected timeframes × walks × combo count (combo count derived from the on-disk `config/wfo.yaml`).
- **Presets** stored as JSON files under `runtime/wfo/presets/<name>.json`. A preset dropdown sits at the top of the form.

### 5.5 Active Overrides panel

Read-only summary of `runtime/wfo/active/live_overrides.yaml`:

| symbol | timeframe | setup | params (compact) | sourced from run | approved at | actions |
|--------|-----------|-------|-------------------|-------------------|-------------|---------|
| AAPL | 15Min | price_discovery | stop=1.25, R=2.0, … | 2026-05-21T14-02_a3f1 | 15:11 UTC | [Revert] [Open run] |

Plus a small **Audit trail** expander showing the tail of `audit.jsonl` (last 50 entries).

A banner at the top reminds the operator: *"Changes here take effect on the next trader restart."*

---

## 6. Cross-cutting concerns

### 6.1 Credentials & docker-compose

Add `env_file: ./config/.env` to the `dashboard` service so it has Alpaca paper-trading creds for universe scan, bar fetch, and (transitively, via subprocess) running WFO. Drop `TRADING_ENV=test` from the dashboard environment block — it was added to bypass `main.py`'s lock-file `sys.exit`, but the dashboard process never imports `main.py`, making the variable a no-op today. Removing it makes "what env is loaded" deterministic from the env file.

`broker/alpaca_client.py` is read-only from the dashboard — universe scan + bar fetch are both `GET`. No order-submitting code path is reachable from the dashboard process.

### 6.2 Error handling & UX

| Failure | Surface | Recovery |
|---|---|---|
| Alpaca API down during preview | Spinner → red banner with the API error; form retains values | Click "Preview" again; or launch anyway (the WFO subprocess will retry universe scan) |
| Disk full / write fails on tmp+replace | Approve action errors with red banner; `audit.jsonl` not written; active file untouched | Free disk, retry |
| Subprocess crashes mid-run | Supervisor sees non-zero exit; job → `failed` with `error=stderr-tail`; partial `results.parquet` retained | Re-launch (deterministic `run_id` resumes) |
| Cancel button hit | SIGTERM → 10 s grace → SIGKILL; manifest `status="interrupted"` | Re-launch or discard |
| Dashboard restart mid-run | Orphan in `jobs/active/` → marked `failed` with `error="orphaned"` on next boot | Re-launch (resumes) |
| Two dashboards on same volume | flock on `supervisor.lock` — second container blocks; first wins. Logged. | Stop the duplicate |
| Approve while another approve is in flight | `os.replace` is atomic; last writer wins. Audit ledger preserves both attempts. | Re-open Active panel to see actual state |
| Gate-failing approve via stale UI | Server-side validation rejects with a red banner | Refresh and re-evaluate |

`st.error` for red banners, `st.success` for confirmations, `st.warning` for soft warnings.

### 6.3 Testing strategy

Suite stays under 30 s; existing 178 tests must continue to pass. Six new test files in `tests/`:

1. **`test_wfo_job_state.py`** — pure-function tests for the job-file state machine: transitions, atomic moves between `queue/` → `active/` → `history/`, orphan detection on boot.
2. **`test_wfo_supervisor.py`** — supervisor with a stubbed subprocess factory: enqueue 2 jobs → both run sequentially; cancel mid-flight → SIGTERM sent; second job still picked up. `tmp_path` for the runtime dir; no real subprocesses.
3. **`test_wfo_active_overrides.py`** — approval action: per-symbol approve writes correct YAML, preserves other symbols, appends to audit log; revert removes entry; gate-failed candidates rejected. `os.replace` atomicity (write succeeds even if old file is being read).
4. **`test_wfo_form_payload.py`** — form payload merges into the `wfo.yaml` template correctly: top-N scan vs explicit symbols, allowlist forces inclusion, blocklist excludes, windowing/gate values overlay cleanly without mutating canonical YAML.
5. **`test_wfo_charts.py`** — chart-data-prep helpers (pure functions): given a tiny `results.parquet` fixture, `walk_equity_curve(df, symbol)`, `is_vs_oos_scatter(df, symbol)`, `param_heatmap(df, symbol, axes)` return the expected DataFrames. Plotly calls themselves are not tested.
6. **`test_main_overrides.py`** (existing) — extended: assert `main.py` reads from `runtime/wfo/active/live_overrides.yaml`, not `latest/`. Verify `_provenance` keys are ignored by `apply_overrides`.

The dashboard rendering is not unit-tested — Streamlit's `AppTest` framework is heavy and brittle. Manual smoke is the verification path: `docker compose up dashboard`, click through.

### 6.4 File-by-file change list

**New files:**
- `ui/wfo/__init__.py`
- `ui/wfo/tab.py` — entry point, panel switcher, session-state plumbing
- `ui/wfo/forms.py` — New Run form
- `ui/wfo/runs_list.py` — Runs table + active-job banner
- `ui/wfo/run_detail.py` — symbol table + approval buttons
- `ui/wfo/charts.py` — Plotly chart builders + pure data-prep helpers
- `ui/wfo/approval.py` — read/write `active/live_overrides.yaml` + audit log
- `ui/wfo/supervisor.py` — singleton thread, subprocess management
- `ui/wfo/job_state.py` — job-file state machine
- `tests/test_wfo_job_state.py`
- `tests/test_wfo_supervisor.py`
- `tests/test_wfo_active_overrides.py`
- `tests/test_wfo_form_payload.py`
- `tests/test_wfo_charts.py`

**Modified:**
- `ui/dashboard.py` — add WFO tab, import `ui.wfo.tab.render`
- `main.py` — change override path: `runtime/wfo/latest/live_overrides.yaml` → `runtime/wfo/active/live_overrides.yaml`
- `config/settings.yaml` — update `overrides.path` to the new active path
- `scripts/run_wfo.py` — remove the `update_latest_symlink_if_passing(...)` call
- `backtest/wfo/report.py` — remove `update_latest_symlink_if_passing` (dead code)
- `docker-compose.yml` — add `env_file: ./config/.env` to dashboard service; drop `TRADING_ENV=test`
- `tests/test_main_overrides.py` — point to new path; assert `_provenance` ignored

(Plotly is already pinned in `requirements.txt`; no dependency change.)

**Removed (run-time effect, not on-disk cleanup):**
- `runtime/wfo/latest` symlink — no longer written by anything; not deleted on deploy. A short note in the operator README points at the new path.

---

## 7. Open questions deferred to v2

- **Hot-reload of overrides** — not in v1 (next-restart-only). Would require parameter-swap-aware logic in `core/position_manager.py` for open positions.
- **Bar-level OOS equity curves** — engine extension to persist per-bar OOS equity per walk. Useful for finer drawdown analysis.
- **Approval audit UI** — beyond a tail view of `audit.jsonl`. Search/filter, per-symbol history view.
- **Multi-operator authentication** — Streamlit auth, role-based approval gates.
- **Per-run retention / cleanup** — automatic pruning of old `runtime/wfo/<run_id>/` dirs.
- **Pre-completion approval** — approving a candidate before the run finishes (e.g., approve symbols whose walks are all done while others continue).
- **Approval workflow UX** — bulk "approve all gate-passing" action, sticky reject persisted per-run.

---

## 8. References

- Existing WFO design: `docs/superpowers/specs/2026-05-19-walk-forward-optimization-design.md`.
- Existing WFO modules: `backtest/wfo/{windowing,grid,fitness,universe,runner,report}.py`.
- Existing dashboard: `ui/dashboard.py`, `ui/logs_panel.py`.
- Live engine entry point: `main.py`'s `apply_overrides(cfg, overrides_path)` and `build_setups(cfg, symbol)`.
