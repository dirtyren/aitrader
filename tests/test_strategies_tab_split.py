"""Tests for the equity/crypto strategies tab split helpers."""
from __future__ import annotations

from pathlib import Path

import pytest

from ui.data.strategy_configs import list_by_asset_class


@pytest.fixture
def yaml_dir(tmp_path) -> Path:
    """Three minimal YAMLs: one equity, one crypto, one with both (defensive)."""
    (tmp_path / "settings_orb_equity.yaml").write_text(
        "system:\n"
        "  name: orb_equity\n"
        "asset_classes:\n"
        "  equity:\n"
        "    symbols: [SPY]\n"
    )
    (tmp_path / "settings_rsi_crypto.yaml").write_text(
        "system:\n"
        "  name: rsi_crypto\n"
        "asset_classes:\n"
        "  crypto:\n"
        "    symbols: [BTC/USD]\n"
    )
    (tmp_path / "settings_mixed.yaml").write_text(
        "system:\n"
        "  name: mixed\n"
        "asset_classes:\n"
        "  equity:\n"
        "    symbols: [SPY]\n"
        "  crypto:\n"
        "    symbols: [BTC/USD]\n"
    )
    return tmp_path


def test_list_by_asset_class_equity_only(yaml_dir):
    names = list_by_asset_class("equity", config_dir=yaml_dir)
    assert "orb_equity" in names
    assert "rsi_crypto" not in names


def test_list_by_asset_class_crypto_only(yaml_dir):
    names = list_by_asset_class("crypto", config_dir=yaml_dir)
    assert "rsi_crypto" in names
    assert "orb_equity" not in names


def test_list_by_asset_class_includes_mixed_in_both(yaml_dir, caplog):
    eq = list_by_asset_class("equity", config_dir=yaml_dir)
    cr = list_by_asset_class("crypto", config_dir=yaml_dir)
    assert "mixed" in eq
    assert "mixed" in cr


def test_list_by_asset_class_invalid_raises(yaml_dir):
    with pytest.raises(ValueError):
        list_by_asset_class("options", config_dir=yaml_dir)


# ---------------------------------------------------------------------------
# format_pnl_inline tests (P&L colorization for admin table cells)
# ---------------------------------------------------------------------------

from ui.components.kpi_row import format_pnl_inline


def test_format_pnl_inline_positive_uses_pos_class():
    out = format_pnl_inline(5.0, fmt="{:+.2f}")
    assert "pnl-pos" in out
    assert "+5.00" in out


def test_format_pnl_inline_negative_uses_neg_class():
    out = format_pnl_inline(-3.5, fmt="{:+.2f}")
    assert "pnl-neg" in out
    assert "-3.50" in out


def test_format_pnl_inline_zero_treated_as_neutral():
    out = format_pnl_inline(0.0, fmt="{:+.2f}")
    assert "pnl-neu" in out


def test_format_pnl_inline_none_returns_em_dash_neutral():
    out = format_pnl_inline(None)
    assert "—" in out
    assert "pnl-neu" in out


def test_format_pnl_inline_nan_returns_em_dash_neutral():
    import math
    out = format_pnl_inline(math.nan)
    assert "—" in out
    assert "pnl-neu" in out
