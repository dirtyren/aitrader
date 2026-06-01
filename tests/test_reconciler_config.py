"""Tests for ReconcilerConfig env loading."""
from __future__ import annotations

import pytest

from reconciler.config import ReconcilerConfig


def test_defaults_when_env_unset(monkeypatch):
    for var in (
        "RECONCILE_INTERVAL_S",
        "RECONCILE_STRIKE_THRESHOLD",
        "RECONCILE_STRIKE_MIN_GAP_S",
        "RECONCILE_QTY_EPS",
        "SHADOW_MODE",
        "RECONCILE_STATE_FILE",
        "RECONCILE_HEARTBEAT_STALE_AFTER_S",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("RECONCILER_ASSET_CLASS", "equity")
    cfg = ReconcilerConfig.from_env()
    assert cfg.interval_s == 30
    assert cfg.strike_threshold == 3
    assert cfg.strike_min_gap_s == 60
    assert cfg.qty_eps == pytest.approx(1e-6)
    assert cfg.shadow_mode is False
    assert cfg.state_file_path == "/app/runtime/reconciler_state.json"
    assert cfg.heartbeat_stale_after_s == 300
    assert cfg.asset_class == "equity"


def test_overrides_from_env(monkeypatch):
    monkeypatch.setenv("RECONCILER_ASSET_CLASS", "crypto")
    monkeypatch.setenv("RECONCILE_INTERVAL_S", "10")
    monkeypatch.setenv("RECONCILE_STRIKE_THRESHOLD", "5")
    monkeypatch.setenv("RECONCILE_STRIKE_MIN_GAP_S", "120")
    monkeypatch.setenv("RECONCILE_QTY_EPS", "0.001")
    monkeypatch.setenv("SHADOW_MODE", "true")
    monkeypatch.setenv("RECONCILE_STATE_FILE", "/tmp/state.json")
    monkeypatch.setenv("RECONCILE_HEARTBEAT_STALE_AFTER_S", "60")
    cfg = ReconcilerConfig.from_env()
    assert cfg.interval_s == 10
    assert cfg.strike_threshold == 5
    assert cfg.strike_min_gap_s == 120
    assert cfg.qty_eps == pytest.approx(0.001)
    assert cfg.shadow_mode is True
    assert cfg.state_file_path == "/tmp/state.json"
    assert cfg.heartbeat_stale_after_s == 60
    assert cfg.asset_class == "crypto"


def test_shadow_mode_truthy_strings(monkeypatch):
    monkeypatch.setenv("RECONCILER_ASSET_CLASS", "equity")
    for value in ("true", "1", "yes", "TRUE", "YES"):
        monkeypatch.setenv("SHADOW_MODE", value)
        assert ReconcilerConfig.from_env().shadow_mode is True
    for value in ("false", "0", "no", "", "off"):
        monkeypatch.setenv("SHADOW_MODE", value)
        assert ReconcilerConfig.from_env().shadow_mode is False
