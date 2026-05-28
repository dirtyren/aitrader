# Strategy Configuration Tab Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a read-only "Configuration" tab to the aitrader Streamlit dashboard that lists every strategy (sourced from YAML configs and the MySQL `strategies` table) and shows its assets, risk, setups, filters, broker, and backtest config in curated sections plus a raw-YAML expander.

**Architecture:** A new pure-Python module `ui/data/strategy_configs.py` parses `config/settings*.yaml` and merges with `trades_repo.list_strategies()` to produce a `list[StrategyEntry]`. A new Streamlit module `ui/tabs/config_tab.py` renders a sidebar list + detail pane. `ui/dashboard.py` wires the new tab between Live Trading and Logs, using the existing `_safe_render` wrapper.

**Tech Stack:** Python 3.12, Streamlit, PyYAML (already a dep), pytest.

**Spec:** `docs/superpowers/specs/2026-05-28-strategy-config-tab-design.md`

---

## File Structure

| File | Responsibility |
|---|---|
| `ui/data/strategy_configs.py` (new) | Pure I/O + parsing. Discover YAMLs, parse into dataclasses, merge with DB list. No Streamlit imports. |
| `ui/tabs/config_tab.py` (new) | Streamlit rendering: sidebar list + detail pane sections. Stateless except for radio key. |
| `ui/dashboard.py` (modify) | Add the Configuration tab to `st.tabs(...)` and route through `_safe_render`. |
| `tests/ui/test_strategy_configs.py` (new) | Unit tests for `ui/data/strategy_configs.py` using `tmp_path` YAML fixtures and `monkeypatch` for the DB call. |

The Streamlit tab is intentionally kept thin — all logic lives in the data module so it can be tested without spinning up Streamlit.

---

## Task 1: Define dataclasses and module skeleton (test-first)

**Files:**
- Create: `ui/data/strategy_configs.py`
- Test: `tests/ui/test_strategy_configs.py`

- [ ] **Step 1: Write the failing test for module imports and types**

Create `tests/ui/test_strategy_configs.py`:

```python
"""Unit tests for ui.data.strategy_configs."""
from __future__ import annotations

from pathlib import Path

import pytest


def test_module_exports_expected_names():
    from ui.data import strategy_configs as sc

    assert hasattr(sc, "AssetClass")
    assert hasattr(sc, "Setup")
    assert hasattr(sc, "StrategyConfig")
    assert hasattr(sc, "StrategyEntry")
    assert hasattr(sc, "load_yaml_configs")
    assert hasattr(sc, "discover_strategies")


def test_assetclass_is_frozen_dataclass():
    from ui.data.strategy_configs import AssetClass

    a = AssetClass(
        name="equity",
        symbols=["SPY"],
        session_open_local="09:30",
        session_close_local="16:00",
        timezone="America/New_York",
        slippage_bps=2.0,
        commission_bps=None,
        commission_per_share=0.0,
    )
    assert a.name == "equity"
    with pytest.raises((AttributeError, Exception)):
        a.name = "crypto"  # type: ignore[misc]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/ui/test_strategy_configs.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'ui.data.strategy_configs'`.

- [ ] **Step 3: Create the module skeleton**

Create `ui/data/strategy_configs.py`:

```python
"""Read-only strategy configuration discovery for the dashboard.

Globs `config/settings*.yaml`, parses each into typed dataclasses, and
merges the result with strategies registered in MySQL so the UI can
distinguish active / defined / db-only strategies.

No Streamlit imports — pure I/O + parsing, unit-testable.
"""
from __future__ import annotations

import glob
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml

logger = logging.getLogger("dashboard")


@dataclass(frozen=True)
class AssetClass:
    name: str
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
    params: dict[str, Any]


@dataclass(frozen=True)
class StrategyConfig:
    name: str
    version: str | None
    env: str | None
    yaml_path: Path
    asset_classes: list[AssetClass]
    risk: dict[str, Any]
    setups: list[Setup]
    filters: dict[str, Any]
    broker: dict[str, Any]
    backtest: dict[str, Any]
    raw: dict


@dataclass(frozen=True)
class StrategyEntry:
    name: str
    status: Literal["active", "defined", "db-only"]
    config: StrategyConfig | None


@dataclass
class _LoadResult:
    configs: dict[str, StrategyConfig] = field(default_factory=dict)
    conflicts: list[tuple[str, list[Path]]] = field(default_factory=list)
    parse_errors: list[tuple[Path, str]] = field(default_factory=list)


def load_yaml_configs(config_dir: Path = Path("config")) -> dict[str, StrategyConfig]:
    raise NotImplementedError


def discover_strategies(config_dir: Path = Path("config")) -> list[StrategyEntry]:
    raise NotImplementedError
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/ui/test_strategy_configs.py -v`
Expected: PASS for the two tests above.

- [ ] **Step 5: Commit**

```bash
git add ui/data/strategy_configs.py tests/ui/test_strategy_configs.py
git commit -m "feat(dashboard): scaffold strategy_configs module + dataclasses"
```

---

## Task 2: Implement `load_yaml_configs` happy path (single YAML)

**Files:**
- Modify: `ui/data/strategy_configs.py`
- Test: `tests/ui/test_strategy_configs.py`

- [ ] **Step 1: Add a fixture helper and the happy-path test**

Append to `tests/ui/test_strategy_configs.py`:

```python
import textwrap


def _write_yaml(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(body).lstrip())
    return path


_ORB_YAML = """
system:
  name: orb_trader
  version: "1.0.0"
  trading_env: paper
asset_classes:
  equity:
    symbols: [SPY, QQQ, IWM]
    session_open_local: "09:30"
    session_close_local: "16:00"
    timezone: America/New_York
    slippage_bps: 2
    commission_per_share: 0.0
  crypto:
    symbols: [BTC/USD, ETH/USD]
    session_open_local: "00:00"
    session_close_local: "23:59"
    timezone: UTC
    slippage_bps: 5
    commission_bps: 25
risk:
  max_risk_per_trade: 0.005
  max_concurrent_positions: 4
  circuit_breaker:
    daily_loss_limit_1: 0.015
    daily_loss_limit_2: 0.025
setups:
  breakout:
    enabled: true
    atr_mult_stop: 1.0
    target_R: 2.0
  mean_reversion:
    enabled: false
    atr_mult_stop: 0.75
filters:
  opening_blackout_min: 15
broker:
  paper_trading: true
backtest:
  start: "2024-01-01"
  end: "2026-04-30"
  initial_equity: 100000
"""


def test_load_yaml_configs_parses_single_yaml(tmp_path):
    from ui.data.strategy_configs import load_yaml_configs

    cfg_dir = tmp_path / "config"
    _write_yaml(cfg_dir / "settings_orb.yaml", _ORB_YAML)

    result = load_yaml_configs(cfg_dir)

    assert list(result.keys()) == ["orb_trader"]
    cfg = result["orb_trader"]
    assert cfg.name == "orb_trader"
    assert cfg.version == "1.0.0"
    assert cfg.env == "paper"
    assert cfg.yaml_path == cfg_dir / "settings_orb.yaml"

    asset_names = sorted(a.name for a in cfg.asset_classes)
    assert asset_names == ["crypto", "equity"]
    eq = next(a for a in cfg.asset_classes if a.name == "equity")
    assert eq.symbols == ["SPY", "QQQ", "IWM"]
    assert eq.timezone == "America/New_York"
    assert eq.slippage_bps == 2
    assert eq.commission_per_share == 0.0
    assert eq.commission_bps is None

    assert cfg.risk["max_risk_per_trade"] == 0.005
    assert cfg.risk["circuit_breaker.daily_loss_limit_1"] == 0.015

    setups_by_name = {s.name: s for s in cfg.setups}
    assert setups_by_name["breakout"].enabled is True
    assert setups_by_name["breakout"].params == {"atr_mult_stop": 1.0, "target_R": 2.0}
    assert setups_by_name["mean_reversion"].enabled is False

    assert cfg.filters == {"opening_blackout_min": 15}
    assert cfg.broker == {"paper_trading": True}
    assert cfg.backtest["start"] == "2024-01-01"

    assert cfg.raw["system"]["name"] == "orb_trader"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/ui/test_strategy_configs.py::test_load_yaml_configs_parses_single_yaml -v`
Expected: FAIL with `NotImplementedError`.

- [ ] **Step 3: Implement `load_yaml_configs` happy path**

Replace the body of `load_yaml_configs` and add helpers to `ui/data/strategy_configs.py`:

```python
def _flatten(d: dict, prefix: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in d.items():
        key = f"{prefix}{k}"
        if isinstance(v, dict):
            out.update(_flatten(v, prefix=f"{key}."))
        else:
            out[key] = v
    return out


def _coerce_symbols(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(s) for s in value]
    if isinstance(value, str):
        logger.warning("symbols field is a string, coercing to single-element list: %r", value)
        return [value]
    logger.warning("symbols field has unsupported type %s, treating as empty", type(value).__name__)
    return []


def _build_asset_classes(raw: dict) -> list[AssetClass]:
    block = raw.get("asset_classes") or {}
    out: list[AssetClass] = []
    for name, body in block.items():
        body = body or {}
        out.append(AssetClass(
            name=str(name),
            symbols=_coerce_symbols(body.get("symbols")),
            session_open_local=body.get("session_open_local"),
            session_close_local=body.get("session_close_local"),
            timezone=body.get("timezone"),
            slippage_bps=body.get("slippage_bps"),
            commission_bps=body.get("commission_bps"),
            commission_per_share=body.get("commission_per_share"),
        ))
    return out


def _build_setups(raw: dict) -> list[Setup]:
    block = raw.get("setups") or {}
    out: list[Setup] = []
    for name, body in block.items():
        body = dict(body or {})
        enabled = bool(body.pop("enabled", False))
        out.append(Setup(name=str(name), enabled=enabled, params=body))
    return out


def _build_config(yaml_path: Path, raw: dict) -> StrategyConfig:
    system = raw.get("system") or {}
    name = system.get("name") or yaml_path.stem.removeprefix("settings_") or yaml_path.stem
    return StrategyConfig(
        name=str(name),
        version=str(system["version"]) if system.get("version") is not None else None,
        env=system.get("trading_env"),
        yaml_path=yaml_path,
        asset_classes=_build_asset_classes(raw),
        risk=_flatten(raw.get("risk") or {}),
        setups=_build_setups(raw),
        filters=dict(raw.get("filters") or {}),
        broker=dict(raw.get("broker") or {}),
        backtest=dict(raw.get("backtest") or {}),
        raw=dict(raw),
    )


def _load(config_dir: Path) -> _LoadResult:
    result = _LoadResult()
    paths = sorted(Path(p) for p in glob.glob(str(config_dir / "settings*.yaml")))
    by_name: dict[str, list[Path]] = {}
    for path in paths:
        try:
            raw = yaml.safe_load(path.read_text()) or {}
        except yaml.YAMLError as e:
            logger.error("Failed to parse %s: %s", path, e)
            result.parse_errors.append((path, str(e)))
            continue
        cfg = _build_config(path, raw)
        by_name.setdefault(cfg.name, []).append(path)
        if cfg.name not in result.configs:
            result.configs[cfg.name] = cfg
    for name, paths_for_name in by_name.items():
        if len(paths_for_name) > 1:
            logger.warning("Duplicate strategy name %r in %s — using first", name, paths_for_name)
            result.conflicts.append((name, paths_for_name))
    return result


def load_yaml_configs(config_dir: Path = Path("config")) -> dict[str, StrategyConfig]:
    return _load(config_dir).configs
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/ui/test_strategy_configs.py -v`
Expected: PASS for all three tests so far (module exports, frozen dataclass, parse-single-yaml).

- [ ] **Step 5: Commit**

```bash
git add ui/data/strategy_configs.py tests/ui/test_strategy_configs.py
git commit -m "feat(dashboard): parse strategy YAML into typed config"
```

---

## Task 3: Edge cases for `load_yaml_configs`

**Files:**
- Modify: `tests/ui/test_strategy_configs.py`

This task adds tests for behavior already implemented in Task 2 (filename fallback, duplicate names, malformed YAML, missing `symbols`). The implementation should already pass; if any test fails, fix the implementation and re-run.

- [ ] **Step 1: Add the edge-case tests**

Append to `tests/ui/test_strategy_configs.py`:

```python
def test_load_yaml_configs_falls_back_to_filename_stem(tmp_path):
    from ui.data.strategy_configs import load_yaml_configs

    cfg_dir = tmp_path / "config"
    _write_yaml(
        cfg_dir / "settings_no_name.yaml",
        """
        risk:
          max_risk_per_trade: 0.01
        """,
    )

    result = load_yaml_configs(cfg_dir)

    assert list(result.keys()) == ["no_name"]
    assert result["no_name"].name == "no_name"
    assert result["no_name"].risk == {"max_risk_per_trade": 0.01}


def test_load_yaml_configs_duplicate_name_keeps_first_alphabetically(tmp_path):
    from ui.data.strategy_configs import load_yaml_configs

    cfg_dir = tmp_path / "config"
    _write_yaml(
        cfg_dir / "settings_a_dup.yaml",
        """
        system: {name: dup_strategy}
        risk: {max_risk_per_trade: 0.01}
        """,
    )
    _write_yaml(
        cfg_dir / "settings_b_dup.yaml",
        """
        system: {name: dup_strategy}
        risk: {max_risk_per_trade: 0.99}
        """,
    )

    result = load_yaml_configs(cfg_dir)
    assert list(result.keys()) == ["dup_strategy"]
    assert result["dup_strategy"].risk == {"max_risk_per_trade": 0.01}


def test_load_yaml_configs_skips_malformed(tmp_path, caplog):
    import logging
    from ui.data.strategy_configs import load_yaml_configs

    cfg_dir = tmp_path / "config"
    _write_yaml(cfg_dir / "settings_bad.yaml", "not: [valid: yaml: at: all")
    _write_yaml(
        cfg_dir / "settings_good.yaml",
        """
        system: {name: good_one}
        """,
    )

    with caplog.at_level(logging.ERROR, logger="dashboard"):
        result = load_yaml_configs(cfg_dir)

    assert list(result.keys()) == ["good_one"]
    assert any("Failed to parse" in rec.message for rec in caplog.records)


def test_load_yaml_configs_missing_symbols_yields_empty_table(tmp_path):
    from ui.data.strategy_configs import load_yaml_configs

    cfg_dir = tmp_path / "config"
    _write_yaml(
        cfg_dir / "settings_partial.yaml",
        """
        system: {name: partial}
        asset_classes:
          equity:
            session_open_local: "09:30"
        """,
    )

    cfg = load_yaml_configs(cfg_dir)["partial"]
    eq = next(a for a in cfg.asset_classes if a.name == "equity")
    assert eq.symbols == []
    assert eq.session_open_local == "09:30"
```

- [ ] **Step 2: Run the new tests**

Run: `pytest tests/ui/test_strategy_configs.py -v`
Expected: all four new tests PASS.

- [ ] **Step 3: Commit**

```bash
git add tests/ui/test_strategy_configs.py
git commit -m "test(dashboard): edge cases for strategy yaml parsing"
```

---

## Task 4: Implement `discover_strategies`

**Files:**
- Modify: `ui/data/strategy_configs.py`
- Test: `tests/ui/test_strategy_configs.py`

- [ ] **Step 1: Write the failing tests for status merging**

Append to `tests/ui/test_strategy_configs.py`:

```python
def test_discover_strategies_merges_yaml_and_db(tmp_path, monkeypatch):
    from ui.data import strategy_configs as sc

    cfg_dir = tmp_path / "config"
    _write_yaml(cfg_dir / "settings_orb.yaml", "system: {name: orb_trader}")
    _write_yaml(cfg_dir / "settings_def.yaml", "system: {name: defined_only}")

    monkeypatch.setattr(
        sc, "_db_strategies",
        lambda: ["orb_trader", "ghost_strategy"],
    )

    entries = sc.discover_strategies(cfg_dir)

    by_name = {e.name: e for e in entries}
    assert sorted(by_name) == ["defined_only", "ghost_strategy", "orb_trader"]
    assert by_name["orb_trader"].status == "active"
    assert by_name["orb_trader"].config is not None
    assert by_name["defined_only"].status == "defined"
    assert by_name["defined_only"].config is not None
    assert by_name["ghost_strategy"].status == "db-only"
    assert by_name["ghost_strategy"].config is None


def test_discover_strategies_tolerates_db_error(tmp_path, monkeypatch, caplog):
    import logging
    from ui.data import strategy_configs as sc

    cfg_dir = tmp_path / "config"
    _write_yaml(cfg_dir / "settings_orb.yaml", "system: {name: orb_trader}")

    def boom() -> list[str]:
        raise RuntimeError("mysql is down")

    monkeypatch.setattr(sc, "_db_strategies", boom)

    with caplog.at_level(logging.WARNING, logger="dashboard"):
        entries = sc.discover_strategies(cfg_dir)

    assert [e.name for e in entries] == ["orb_trader"]
    assert entries[0].status == "defined"
    assert any("mysql" in rec.message.lower() for rec in caplog.records)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/ui/test_strategy_configs.py -v -k discover`
Expected: FAIL with `NotImplementedError` and `AttributeError: module ... has no attribute '_db_strategies'`.

- [ ] **Step 3: Implement `discover_strategies` and the DB indirection**

Add to `ui/data/strategy_configs.py` (the `_db_strategies` indirection lets tests monkeypatch without importing MySQL):

```python
def _db_strategies() -> list[str]:
    from ui.data.trades_repo import list_strategies
    return list(list_strategies())


def discover_strategies(config_dir: Path = Path("config")) -> list[StrategyEntry]:
    configs = load_yaml_configs(config_dir)
    yaml_names = set(configs.keys())
    try:
        db_names = set(_db_strategies())
    except Exception as e:
        logger.warning("MySQL strategies query failed (%s) — DB list treated as empty", e)
        db_names = set()

    entries: list[StrategyEntry] = []
    for name in sorted(yaml_names | db_names):
        in_yaml = name in yaml_names
        in_db = name in db_names
        if in_yaml and in_db:
            status = "active"
        elif in_yaml:
            status = "defined"
        else:
            status = "db-only"
        entries.append(StrategyEntry(
            name=name,
            status=status,
            config=configs.get(name),
        ))
    return entries
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/ui/test_strategy_configs.py -v`
Expected: every test PASSES (8 tests total in this file).

- [ ] **Step 5: Commit**

```bash
git add ui/data/strategy_configs.py tests/ui/test_strategy_configs.py
git commit -m "feat(dashboard): merge yaml + db strategy lists with status badges"
```

---

## Task 5: Build the Configuration tab UI

**Files:**
- Create: `ui/tabs/config_tab.py`

No unit tests for this file (matches existing `ui/tabs/` convention — Streamlit code is exercised through the running dashboard). Manual smoke test in Task 7.

- [ ] **Step 1: Create the tab module**

Create `ui/tabs/config_tab.py`:

```python
"""Configuration tab — read-only viewer for strategy YAML + DB membership.

Sidebar list of every discovered strategy (YAML ∪ MySQL) with a status
badge, detail pane on the right showing curated sections plus a raw
YAML expander.
"""
from __future__ import annotations

from typing import Any

import pandas as pd
import streamlit as st
import yaml

from ui.data.strategy_configs import (
    AssetClass,
    Setup,
    StrategyConfig,
    StrategyEntry,
    discover_strategies,
)


def render() -> None:
    st.subheader("Configuration — Strategy Definitions")

    entries = discover_strategies()
    if not entries:
        st.info("No strategies configured yet.")
        return

    left, right = st.columns([1, 3], gap="large")

    with left:
        labels = [_format_sidebar_label(e) for e in entries]
        idx = st.radio(
            "Strategy",
            options=list(range(len(entries))),
            format_func=lambda i: labels[i],
            key="config_selected_strategy",
        )

    selected = entries[idx]
    with right:
        if selected.status == "db-only":
            _render_db_only(selected.name)
        else:
            assert selected.config is not None
            _render_detail(selected.status, selected.config)


def _format_sidebar_label(entry: StrategyEntry) -> str:
    if entry.config is None:
        return f"{entry.name}\n[{entry.status}]"
    env = entry.config.env or "—"
    summary_parts = [
        f"{ac.name}·{len(ac.symbols)}" for ac in entry.config.asset_classes
    ]
    summary = " / ".join(summary_parts) if summary_parts else "no assets"
    return f"{entry.name}\n[{entry.status}] {env} · {summary}"


def _render_db_only(name: str) -> None:
    st.markdown(f"### {name}")
    st.caption("Status: **db-only**")
    st.info("No YAML config found for this strategy. See Strategies tab for trade history.")


def _render_detail(status: str, cfg: StrategyConfig) -> None:
    _render_header(status, cfg)
    _render_assets(cfg.asset_classes)
    _render_risk(cfg.risk)
    _render_setups(cfg.setups)
    _render_filters(cfg.filters)
    _render_broker(cfg.broker)
    _render_backtest(cfg.backtest)
    _render_raw(cfg.raw)


def _render_header(status: str, cfg: StrategyConfig) -> None:
    st.markdown(f"### {cfg.name}")
    st.caption(
        f"Status: **{status}** · Version: **{cfg.version or '—'}** · "
        f"Env: **{cfg.env or '—'}** · YAML: `{cfg.yaml_path}`"
    )


def _render_assets(asset_classes: list[AssetClass]) -> None:
    st.markdown("#### Assets")
    if not asset_classes:
        st.caption("No asset classes defined.")
        return
    for ac in asset_classes:
        st.markdown(f"**{ac.name}** ({len(ac.symbols)} symbols)")
        if not ac.symbols:
            st.caption("No symbols configured.")
            continue
        session = (
            f"{ac.session_open_local or '—'}–{ac.session_close_local or '—'}"
        )
        rows = [{
            "Symbol": s,
            "Session": session,
            "Timezone": ac.timezone or "—",
            "Slippage (bps)": ac.slippage_bps,
            "Commission (bps)": ac.commission_bps,
            "Commission/share": ac.commission_per_share,
        } for s in ac.symbols]
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)


def _kv_table(d: dict[str, Any]) -> None:
    if not d:
        st.caption("Empty.")
        return
    rows = [{"Key": k, "Value": _scalar(v)} for k, v in d.items()]
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)


def _scalar(v: Any) -> Any:
    if isinstance(v, (dict, list)):
        return yaml.safe_dump(v, default_flow_style=True).strip()
    return v


def _render_risk(risk: dict[str, Any]) -> None:
    st.markdown("#### Risk")
    _kv_table(risk)


def _render_setups(setups: list[Setup]) -> None:
    st.markdown("#### Setups")
    if not setups:
        st.caption("No setups defined.")
        return
    sorted_setups = sorted(setups, key=lambda s: (not s.enabled, s.name))
    rows = [{
        "Setup": s.name,
        "Enabled": "✓" if s.enabled else "✗",
        "Parameters": yaml.safe_dump(s.params, default_flow_style=True).strip() if s.params else "—",
    } for s in sorted_setups]
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)


def _render_filters(filters: dict[str, Any]) -> None:
    st.markdown("#### Filters")
    _kv_table(filters)


def _render_broker(broker: dict[str, Any]) -> None:
    st.markdown("#### Broker")
    _kv_table(broker)


def _render_backtest(backtest: dict[str, Any]) -> None:
    st.markdown("#### Backtest")
    _kv_table(backtest)


def _render_raw(raw: dict) -> None:
    with st.expander("Raw YAML", expanded=False):
        st.code(yaml.safe_dump(raw, sort_keys=False), language="yaml")
```

- [ ] **Step 2: Smoke-test the import**

Run: `python -c "from ui.tabs.config_tab import render; print('ok')"`
Expected output: `ok`

- [ ] **Step 3: Commit**

```bash
git add ui/tabs/config_tab.py
git commit -m "feat(dashboard): configuration tab UI (sidebar + detail pane)"
```

---

## Task 6: Wire the new tab into `ui/dashboard.py`

**Files:**
- Modify: `ui/dashboard.py`

- [ ] **Step 1: Add the import and tab**

In `ui/dashboard.py`, change the imports block to add `config_tab`:

Find:
```python
from ui.tabs import live_tab, strategies_tab
```
Replace with:
```python
from ui.tabs import config_tab, live_tab, strategies_tab
```

Find:
```python
strategies_t, live_t, logs_t, wfo_t = st.tabs([
    "Strategies", "Live Trading", "Logs", "WFO",
])
```
Replace with:
```python
strategies_t, live_t, config_t, logs_t, wfo_t = st.tabs([
    "Strategies", "Live Trading", "Configuration", "Logs", "WFO",
])
```

Insert immediately after the `with live_t:` block (before `with logs_t:`):
```python
with config_t:
    _safe_render("config", config_tab.render)
```

- [ ] **Step 2: Smoke-test the import**

Run: `python -c "import ui.dashboard"  2>&1 | tail -5`
Expected: any Streamlit `bare-mode` warning is fine, but no `ImportError` or `NameError`.

- [ ] **Step 3: Commit**

```bash
git add ui/dashboard.py
git commit -m "feat(dashboard): wire Configuration tab between Live Trading and Logs"
```

---

## Task 7: Full test suite + manual smoke test

**Files:**
- None modified.

- [ ] **Step 1: Run the full test suite**

Run: `pytest -q`
Expected: all tests pass, including the 8 new ones in `tests/ui/test_strategy_configs.py`.

If any pre-existing test fails, do not modify it — investigate whether the new code touched something it shouldn't have. The new module is pure-additive; there should be no regressions.

- [ ] **Step 2: Build and run the dashboard container**

Run: `docker compose up -d --build dashboard`
Expected: container comes up healthy.

- [ ] **Step 3: Manual smoke test (browser)**

Open the dashboard URL, check:

1. A new **Configuration** tab is visible between Live Trading and Logs.
2. The sidebar lists every strategy from `config/settings*.yaml`. Each entry shows a status badge.
3. Clicking `vwap_wave` (or `orb_trader`) renders:
   - header with version, env, YAML path
   - assets tables for each asset class with symbol counts in the section title
   - risk, setups (with enabled ✓/✗, sorted enabled-first), filters, broker, backtest tables
   - "Raw YAML" expander that contains the full file body
4. Selecting another strategy keeps the selection across the next 5s autorefresh of other tabs (Configuration tab itself does not autorefresh, but session state is shared across tabs).
5. Stop MySQL (or simulate via misconfigured creds) → reload → tab still renders, every YAML strategy is `defined`, no Python error.

If any check fails, file a follow-up task; do not patch the spec or plan in place.

- [ ] **Step 4: Commit any doc/test fixes from manual testing (if needed)**

If the smoke test reveals real bugs, branch and fix them in a separate commit. Do not amend prior commits.

---

## Self-Review Notes

- **Spec coverage:** §2.1 tab placement → Task 6. §2.2 sidebar layout → Task 5 `_format_sidebar_label`. §2.3 detail sections → Task 5 helpers. §3.2 dataclasses & module contract → Tasks 1, 2, 4. §3.3 tab contract → Task 5. §3.4 conflict surfacing — partial: conflicts are *logged* (Task 2 `_load`) but not displayed inline in the right pane. **This is a deliberate v1 simplification** — the duplicate-name path is a corner case (operators rename strategies infrequently), and surfacing it requires plumbing `_LoadResult` through `discover_strategies`. Logging-only is acceptable for v1; add an in-tab warning later if it becomes a real problem. §3.5 dashboard wiring → Task 6. §4 data flow → covered by Tasks 2 & 4. §5 error handling → Tasks 2, 3, 4. §6 testing → Tasks 1–4 fully cover the spec's test list.
- **Type consistency:** `StrategyConfig.asset_classes: list[AssetClass]` used in both module (Task 1) and UI (Task 5). `StrategyEntry.config: StrategyConfig | None` matches the Task 5 dispatch on `entry.status == "db-only"` to mean `entry.config is None`.
- **No placeholders:** every code step contains complete, runnable code. Every command shows expected output.
