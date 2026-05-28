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
