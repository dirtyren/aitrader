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
