"""Approval module — atomic active-overrides writes and audit log."""
from __future__ import annotations
import json
from pathlib import Path

import pytest
import yaml

from ui.wfo.approval import (
    read_active, approve_symbol, revert_symbol, read_audit_tail,
    GateFailedApproveError,
)


def _candidate_entry(setup="price_discovery", passed=True) -> dict:
    return {
        "timeframe": "15Min", "setup": setup,
        "setup_params": {"atr_mult_stop": 1.25, "target_R": 2.0,
                         "arm_window_bars": 6},
        "position_management": {"max_hold_bars": 12, "breakeven_at_R": 1.0},
        "metadata": {"walks": 30, "wfe": 0.78, "total_oos_pnl": 4213.50,
                     "passed": passed},
    }


def test_read_active_returns_empty_when_missing(tmp_path):
    assert read_active(tmp_path / "missing.yaml") == {"symbols": {}}


def test_approve_symbol_creates_file_with_provenance(tmp_path):
    active = tmp_path / "active.yaml"
    audit = tmp_path / "audit.jsonl"
    approve_symbol(active_path=active, audit_path=audit,
                   symbol="AAPL", candidate=_candidate_entry(),
                   run_id="run_a")
    payload = yaml.safe_load(active.read_text())
    assert "AAPL" in payload["symbols"]
    entry = payload["symbols"]["AAPL"]
    assert entry["setup"] == "price_discovery"
    assert entry["_provenance"]["run_id"] == "run_a"
    assert entry["_provenance"]["approved_by"] == "dashboard"


def test_approve_symbol_preserves_other_symbols(tmp_path):
    active = tmp_path / "active.yaml"
    audit = tmp_path / "audit.jsonl"
    approve_symbol(active, audit, "AAPL", _candidate_entry(), "run_a")
    approve_symbol(active, audit, "TSLA", _candidate_entry(setup="vwap_bounce"),
                   "run_b")
    payload = yaml.safe_load(active.read_text())
    assert set(payload["symbols"].keys()) == {"AAPL", "TSLA"}
    assert payload["symbols"]["TSLA"]["setup"] == "vwap_bounce"


def test_approve_symbol_replaces_prior_entry(tmp_path):
    active = tmp_path / "active.yaml"
    audit = tmp_path / "audit.jsonl"
    approve_symbol(active, audit, "AAPL", _candidate_entry(), "run_a")
    second = _candidate_entry()
    second["setup_params"]["atr_mult_stop"] = 1.50
    approve_symbol(active, audit, "AAPL", second, "run_b")
    payload = yaml.safe_load(active.read_text())
    assert payload["symbols"]["AAPL"]["setup_params"]["atr_mult_stop"] == 1.50
    assert payload["symbols"]["AAPL"]["_provenance"]["run_id"] == "run_b"


def test_approve_gate_failed_raises(tmp_path):
    active = tmp_path / "active.yaml"
    audit = tmp_path / "audit.jsonl"
    with pytest.raises(GateFailedApproveError):
        approve_symbol(active, audit, "AAPL",
                       _candidate_entry(passed=False), "run_a")
    assert not active.exists()


def test_revert_removes_symbol_only(tmp_path):
    active = tmp_path / "active.yaml"
    audit = tmp_path / "audit.jsonl"
    approve_symbol(active, audit, "AAPL", _candidate_entry(), "run_a")
    approve_symbol(active, audit, "TSLA", _candidate_entry(), "run_a")
    revert_symbol(active, audit, "AAPL")
    payload = yaml.safe_load(active.read_text())
    assert list(payload["symbols"].keys()) == ["TSLA"]


def test_revert_missing_symbol_is_idempotent(tmp_path):
    active = tmp_path / "active.yaml"
    audit = tmp_path / "audit.jsonl"
    revert_symbol(active, audit, "AAPL")  # no error
    payload = yaml.safe_load(active.read_text())
    assert payload["symbols"] == {}


def test_audit_log_records_each_action(tmp_path):
    active = tmp_path / "active.yaml"
    audit = tmp_path / "audit.jsonl"
    approve_symbol(active, audit, "AAPL", _candidate_entry(), "run_a")
    revert_symbol(active, audit, "AAPL")
    lines = audit.read_text().strip().splitlines()
    assert len(lines) == 2
    a, b = json.loads(lines[0]), json.loads(lines[1])
    assert a["action"] == "approve" and a["symbol"] == "AAPL"
    assert a["run_id"] == "run_a"
    assert b["action"] == "revert" and b["symbol"] == "AAPL"


def test_read_audit_tail_returns_last_n(tmp_path):
    active = tmp_path / "active.yaml"
    audit = tmp_path / "audit.jsonl"
    for i in range(5):
        approve_symbol(active, audit, f"S{i}", _candidate_entry(), "run_a")
    tail = read_audit_tail(audit, n=3)
    assert [t["symbol"] for t in tail] == ["S2", "S3", "S4"]
