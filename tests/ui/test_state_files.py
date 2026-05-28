import json
from pathlib import Path

import pytest

from ui.data.state_files import get_last_price


def _write_state(tmp_path: Path, strategy: str, payload: dict) -> Path:
    runtime = tmp_path / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    f = runtime / f"trading_state_{strategy}.json"
    f.write_text(json.dumps(payload))
    return f


def test_get_last_price_returns_value(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_state(tmp_path, "rsi_trader", {
        "symbols": [{"symbol": "AAPL", "last_price": 200.5},
                    {"symbol": "MSFT", "last_price": 410.0}],
    })
    assert get_last_price("rsi_trader", "AAPL") == 200.5
    assert get_last_price("rsi_trader", "MSFT") == 410.0


def test_get_last_price_missing_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert get_last_price("nope_strategy", "AAPL") is None


def test_get_last_price_missing_symbol(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_state(tmp_path, "rsi_trader", {"symbols": [{"symbol": "AAPL", "last_price": 200.5}]})
    assert get_last_price("rsi_trader", "TSLA") is None


def test_get_last_price_malformed_json(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "trading_state_bad.json").write_text("{not json")
    assert get_last_price("bad", "AAPL") is None


def test_get_last_price_field_absent_returns_none(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_state(tmp_path, "old_strategy", {"symbols": [{"symbol": "AAPL", "vwap": 100.0}]})
    assert get_last_price("old_strategy", "AAPL") is None
