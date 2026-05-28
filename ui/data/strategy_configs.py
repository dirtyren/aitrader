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
