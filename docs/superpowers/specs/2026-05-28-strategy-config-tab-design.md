# Strategy Configuration Tab — Design

**Date:** 2026-05-28
**Status:** Draft (pending implementation)
**Owner:** dashboard

## 1. Goal

Add a read-only **Configuration** tab to the aitrader dashboard so an operator can see, at a glance, what each strategy is configured to do — its assets, risk parameters, setups, filters, broker, and backtest window — without `cat`-ing YAML files on the host.

Out of scope (explicitly): editing config from the UI, hot-reloading the trader, running scenarios, parameter optimization. Those would be follow-ups.

## 2. UX

### 2.1 Tab placement

Order in `st.tabs([...])` becomes:

```
Strategies | Live Trading | Configuration | Logs | WFO
```

`Configuration` sits between `Live Trading` and `Logs`.

### 2.2 Layout

Sidebar list on the left (radio-style, one selection), detail pane on the right. The sidebar lists every strategy discovered (see §3.2) with three pieces of metadata:

- name (e.g. `vwap_wave`, `orb_trader`, `rsi_crypto_trader`)
- status badge: `active` | `defined` | `db-only` (see §3.2)
- one-line summary: `paper · equity·6 / crypto·5`

The detail pane renders the curated sections in §2.3 for the selected strategy.

### 2.3 Detail pane sections

For a strategy with a YAML config, the detail pane renders, in order:

1. **Header** — name (large), status badge, `version`, `env` (paper/live/backtest), full YAML path.
2. **Assets** — one table per asset class under `asset_classes.*`. Columns: `Symbol`, `Session` (`open–close local`), `Timezone`, `Slippage (bps)`, `Commission`. Asset-class section title includes the symbol count, e.g. `equity (6 symbols)`.
3. **Risk** — flat key/value table from the `risk` block (incl. nested `circuit_breaker` flattened with dotted keys).
4. **Setups** — one row per setup under `setups.*`, columns `Setup`, `Enabled` (✓/✗), `Parameters` (the rest of the setup's keys as a compact JSON-ish dict). Enabled rows sorted first.
5. **Filters** — flat key/value from the `filters` block.
6. **Broker** — flat key/value from the `broker` block.
7. **Backtest window** — `start`, `end`, `initial_equity` from `backtest`.
8. **Raw YAML** — collapsed `st.code(yaml_dump, language="yaml")` expander, so any keys not modeled by sections 2–7 are still visible.

For a `db-only` strategy (no YAML): only the header + an `st.info("No YAML config found for this strategy. See Strategies tab for trade history.")`.

## 3. Architecture

### 3.1 Files

Three new/touched files:

- `ui/data/strategy_configs.py` — pure I/O + parsing. No Streamlit imports. Unit-testable.
- `ui/tabs/config_tab.py` — Streamlit rendering. One public function: `render()`.
- `ui/dashboard.py` — add the tab to the `st.tabs([...])` call and route to `config_tab.render()` via the existing `_safe_render` helper.

### 3.2 `ui/data/strategy_configs.py` contract

```python
@dataclass(frozen=True)
class AssetClass:
    name: str                    # "equity" | "crypto" | ...
    symbols: list[str]
    session_open_local: str | None
    session_close_local: str | None
    timezone: str | None
    slippage_bps: float | None
    commission_bps: float | None
    commission_per_share: float | None

@dataclass(frozen=True)
class Setup:
    name: str
    enabled: bool
    params: dict[str, Any]       # the rest of the setup's keys

@dataclass(frozen=True)
class StrategyConfig:
    name: str                    # system.name, falls back to filename stem
    version: str | None          # system.version
    env: str | None              # system.trading_env
    yaml_path: Path
    asset_classes: list[AssetClass]
    risk: dict[str, Any]         # flattened (dotted keys for nested)
    setups: list[Setup]
    filters: dict[str, Any]
    broker: dict[str, Any]
    backtest: dict[str, Any]
    raw: dict                    # entire parsed YAML body

@dataclass(frozen=True)
class StrategyEntry:
    name: str
    status: Literal["active", "defined", "db-only"]
    config: StrategyConfig | None    # None when status == "db-only"

def load_yaml_configs(config_dir: Path = Path("config")) -> dict[str, StrategyConfig]: ...
def discover_strategies(config_dir: Path = Path("config")) -> list[StrategyEntry]: ...
```

`load_yaml_configs`:

- Globs `config/settings*.yaml`.
- For each file, `yaml.safe_load`, then build a `StrategyConfig`. Name is `system.name` if present, otherwise the filename stem with `settings_` stripped.
- If two files share a `name`, keep the first by sorted path order and append the conflict to a module-level warnings list (logged via `dashboard` logger and surfaced inline in the sidebar — see §3.4).
- Returns `{name: StrategyConfig}`.

`discover_strategies`:

- Calls `load_yaml_configs(config_dir)`.
- Calls `trades_repo.list_strategies()` and tolerates a DB error by logging a warning and treating the DB list as empty (the tab still works in that case, every YAML strategy is `defined`).
- Returns one `StrategyEntry` per name in `sorted(yaml_names | db_names)`:
  - `active`  — YAML + DB
  - `defined` — YAML only
  - `db-only` — DB only

### 3.3 `ui/tabs/config_tab.py` contract

```python
def render() -> None: ...
```

- Calls `discover_strategies()`. If the result is empty, shows `st.info("No strategies configured yet.")`.
- Renders sidebar via two columns (`st.columns([1, 3])`):
  - Left: `st.radio` over the entry list. The label for each option is built by a `_format_sidebar_label(entry)` helper that returns: `f"{entry.name}\n[{entry.status}] {env_str} · {assets_summary}"`, where `env_str` is the YAML `system.trading_env` or `"—"` and `assets_summary` is `equity·N / crypto·M / ...` joined by `/` over `entry.config.asset_classes`. For `db-only` entries the suffix is just `[db-only]`. The radio is bound to `key="config_selected_strategy"` so selection persists across reruns.
  - Right: dispatches on `entry.status` to either `_render_detail(entry.config)` or `_render_db_only(entry.name)`.
- Each section in §2.3 is its own helper `_render_assets`, `_render_risk`, etc., each taking the relevant slice of the config so they can be unit-skipped (no Streamlit-level tests planned, but small helpers keep the file readable).
- Section 2.3.8 (Raw YAML) uses `yaml.safe_dump(config.raw, sort_keys=False)` inside `st.expander("Raw YAML", expanded=False)`.

### 3.4 Conflict surfacing

If `load_yaml_configs` recorded duplicate-name conflicts, `config_tab.render()` shows an `st.warning` at the top of the right pane listing the duplicates (e.g. "Two YAMLs claim `vwap_wave`: `config/settings.yaml`, `config/settings_alt.yaml` — using the first."). Same warnings are written to `logs/dashboard.log` via the existing `dashboard` logger.

### 3.5 `ui/dashboard.py` changes

Single tab insertion:

```python
strategies_t, live_t, config_t, logs_t, wfo_t = st.tabs([
    "Strategies", "Live Trading", "Configuration", "Logs", "WFO",
])

with config_t:
    _safe_render("config", config_tab.render)
```

`_safe_render` already logs tracebacks to `dashboard.log` (PR #50), so any parse failure is captured without crashing the page.

## 4. Data flow

```
config/settings*.yaml        MySQL strategies table
        |                              |
        v                              v
load_yaml_configs()          trades_repo.list_strategies()
        \                            /
         \                          /
          v                        v
         discover_strategies()  -> [StrategyEntry, ...]
                    |
                    v
           config_tab.render()
                    |
                    v
        st.radio + section helpers
```

No caching layer in v1: the tab reads from disk on each rerun. YAMLs are small (<10 KB) and there are <20 of them; this is cheap enough. If profiling shows an issue, add `@st.cache_data(ttl=30)` to `load_yaml_configs` later.

## 5. Error handling

- YAML missing `system.name` → fall back to filename stem (with `settings_` prefix stripped). Log a `WARNING` to `dashboard.log`. Strategy still appears in the sidebar.
- YAML fails to parse (`yaml.YAMLError`) → skip that file, log `ERROR` with file path + exception, surface a sidebar warning row "Failed to parse <path>".
- Asset class block missing `symbols` → render the asset table with zero rows; do not error.
- Asset class block has `symbols` as a non-list → coerce to a list with one element if string, otherwise treat as empty + log warning.
- `trades_repo.list_strategies()` raises (MySQL down) → log warning, proceed with empty DB list (every YAML strategy is `defined`).
- Any uncaught exception in `render()` is caught by `_safe_render` and logged to `dashboard.log`, with an inline error in the tab.

## 6. Testing

New file `tests/ui/test_strategy_configs.py`:

- `test_load_yaml_configs_parses_real_yaml` — points at `config/settings_orb.yaml` (or a fixture copy), asserts:
  - `name == "orb_trader"`, `env == "paper"`, asset classes count, that `setups` is a list with at least one enabled entry, `raw` round-trips back to YAML.
- `test_load_yaml_configs_falls_back_to_filename_when_name_missing` — fixture YAML without `system.name`.
- `test_load_yaml_configs_records_duplicate_name_conflict` — two fixtures with the same `system.name`; assert one wins, conflict is recorded.
- `test_discover_strategies_merges_yaml_and_db` — monkeypatch `trades_repo.list_strategies` to return `["orb_trader", "ghost_strategy"]`, with two YAML fixtures (`orb_trader`, `defined_only`); assert statuses `active` / `defined` / `db-only`, sorted by name.
- `test_discover_strategies_tolerates_db_error` — `list_strategies` raises; assert YAML strategies still returned, all `defined`.

No Streamlit-level tests for `config_tab.py` (matches existing `ui/tabs/` test footprint).

## 7. Acceptance

- New "Configuration" tab appears in the dashboard between Live Trading and Logs.
- Selecting `vwap_wave` shows `version 2.0.0`, `env paper`, the equity + crypto symbol tables, the risk block (incl. circuit breakers), all 4 setups with `enabled` flags, the filters/broker/backtest blocks, and a collapsible Raw YAML.
- Selecting a `db-only` strategy shows the header + the "no YAML" notice without errors.
- Renaming a YAML to share `system.name` with another strategy shows a warning at the top of the right pane and an entry in `dashboard.log`.
- Stopping MySQL still lets the tab render with all YAML strategies marked `defined`.
- Unit tests in `tests/ui/test_strategy_configs.py` all pass.
