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
