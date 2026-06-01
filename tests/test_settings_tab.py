"""Tests for the dashboard Settings tab credential save flow."""
from __future__ import annotations

import pytest

from ui.tabs import settings_tab as st_mod


def test_save_calls_upsert_when_test_passes(monkeypatch):
    upsert_calls: list[tuple] = []
    monkeypatch.setattr(st_mod, "_upsert", lambda *a: upsert_calls.append(a))
    monkeypatch.setattr(
        st_mod, "_test_connection",
        lambda *a, **kw: (True, "ABC1234"),
    )
    monkeypatch.setattr(
        st_mod, "_set_account_number",
        lambda *a: None,
    )
    ok, msg = st_mod.save_credentials(
        asset_class="equity",
        api_key="AK", secret_key="SK",
        base_url="https://paper-api.alpaca.markets",
    )
    assert ok is True
    assert "ABC1234" in msg
    assert len(upsert_calls) == 1
    assert upsert_calls[0] == (
        "equity", "AK", "SK", "https://paper-api.alpaca.markets",
    )


def test_save_blocks_when_test_fails(monkeypatch):
    upsert_calls: list[tuple] = []
    monkeypatch.setattr(st_mod, "_upsert", lambda *a: upsert_calls.append(a))
    monkeypatch.setattr(
        st_mod, "_test_connection",
        lambda *a, **kw: (False, "Invalid API key or secret"),
    )
    ok, msg = st_mod.save_credentials(
        asset_class="equity",
        api_key="bad", secret_key="bad",
        base_url="https://paper-api.alpaca.markets",
    )
    assert ok is False
    assert "Invalid" in msg
    assert upsert_calls == []


def test_containers_for_asset_class_lists_equity_traders(tmp_path, monkeypatch):
    """The save banner lists the trader containers needing a restart."""
    (tmp_path / "settings_orb_equity.yaml").write_text(
        "system:\n  name: orb_equity\nasset_classes:\n  equity:\n    symbols: [SPY]\n"
    )
    (tmp_path / "settings_rsi_crypto.yaml").write_text(
        "system:\n  name: rsi_crypto\nasset_classes:\n  crypto:\n    symbols: [BTC/USD]\n"
    )
    out = st_mod.containers_for_asset_class("equity", config_dir=tmp_path)
    assert any("orb_equity" in c for c in out)
    assert not any("rsi_crypto" in c for c in out)
