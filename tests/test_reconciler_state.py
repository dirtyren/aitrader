"""Tests for reconciler state file (persistent last_orders_check_ts)."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from reconciler.state import load_state, save_state


def test_load_returns_none_when_file_missing(tmp_path):
    path = tmp_path / "missing.json"
    state = load_state(str(path))
    assert state.last_orders_check_ts is None


def test_save_then_load_round_trip(tmp_path):
    path = tmp_path / "state.json"
    ts = datetime(2026, 5, 28, 14, 0, tzinfo=timezone.utc)
    save_state(str(path), last_orders_check_ts=ts)
    loaded = load_state(str(path))
    assert loaded.last_orders_check_ts == ts


def test_load_returns_none_for_corrupt_file(tmp_path):
    path = tmp_path / "corrupt.json"
    path.write_text("not json {")
    state = load_state(str(path))
    assert state.last_orders_check_ts is None


def test_save_is_atomic(tmp_path):
    """save_state must write via a temp file + rename so a crash mid-write
    can never leave a partial file."""
    path = tmp_path / "state.json"
    ts = datetime(2026, 5, 28, 14, 0, tzinfo=timezone.utc)
    save_state(str(path), last_orders_check_ts=ts)
    leftover = list(tmp_path.glob("*.tmp"))
    assert leftover == []
