"""Tests for MySQLStore.migrate_legacy_json — the one-time JSON → MySQL import.

The store's engine is bypassed (the migration helper only calls public methods
load_open_positions and position_opened, which we stub on a subclass).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from state.mysql_store import MySQLStore
from state.position_book import OpenPosition, PositionBook


class _StubStore(MySQLStore):
    """MySQLStore with the SQLAlchemy engine and DB methods stubbed."""

    def __init__(self, *, existing_count: int = 0):
        self.strategy_name = "test_strategy"
        self._strategy_id = 1
        self._log = __import__("logging").getLogger("test")
        self._existing_count = existing_count
        self.opened: list[tuple[OpenPosition, str]] = []

    def load_open_positions(self) -> PositionBook:
        book = PositionBook()
        for i in range(self._existing_count):
            book.add(OpenPosition(
                symbol=f"X{i}", setup="x", side="long", qty=1.0,
                entry_px=1.0, stop_px=None, target_px=None,
                opened_at=datetime.now(timezone.utc), order_id="",
            ))
        return book

    def position_opened(self, pos: OpenPosition, asset_class: str) -> None:
        self.opened.append((pos, asset_class))


def _legacy_payload() -> dict:
    return {
        "version": 1,
        "positions": [
            {
                "symbol": "AAPL", "setup": "price_discovery", "side": "long",
                "qty": 10, "entry_px": 100.0, "stop_px": 99.0, "target_px": 102.0,
                "opened_at": "2026-05-14T14:00:00+00:00",
                "order_id": "o1", "stop_order_id": "leg1",
                "initial_stop_px": 99.0, "breakeven_moved": False,
                "bars_held": 3, "adopted": False,
            },
            {
                "symbol": "BTC/USD", "setup": "vwap_bounce", "side": "long",
                "qty": 0.5, "entry_px": 50000.0, "stop_px": None, "target_px": None,
                "opened_at": "2026-05-14T14:05:00+00:00",
                "order_id": "", "stop_order_id": None,
                "initial_stop_px": None, "breakeven_moved": False,
                "bars_held": 0, "adopted": True,
            },
        ],
    }


def _ac_for(symbol: str) -> str | None:
    if symbol == "AAPL":
        return "equity"
    if symbol in ("BTC/USD", "BTCUSD"):
        return "crypto"
    return None


def test_migrate_imports_rows_and_archives_file(tmp_path: Path):
    json_path = tmp_path / "position_book_test.json"
    json_path.write_text(json.dumps(_legacy_payload()))

    store = _StubStore(existing_count=0)
    imported = store.migrate_legacy_json(json_path, asset_class_for=_ac_for)

    assert imported == 2
    assert {p.symbol for p, _ in store.opened} == {"AAPL", "BTC/USD"}
    assert {ac for _, ac in store.opened} == {"equity", "crypto"}
    assert not json_path.exists()
    assert (tmp_path / "position_book_test.json.migrated").exists()


def test_migrate_noop_when_mysql_already_has_positions(tmp_path: Path):
    json_path = tmp_path / "position_book_test.json"
    json_path.write_text(json.dumps(_legacy_payload()))

    store = _StubStore(existing_count=2)
    imported = store.migrate_legacy_json(json_path, asset_class_for=_ac_for)

    assert imported == 0
    assert store.opened == []
    assert json_path.exists()  # untouched


def test_migrate_noop_when_no_legacy_file(tmp_path: Path):
    store = _StubStore(existing_count=0)
    imported = store.migrate_legacy_json(
        tmp_path / "missing.json", asset_class_for=_ac_for
    )
    assert imported == 0
    assert store.opened == []


def test_migrate_skips_unknown_asset_class(tmp_path: Path):
    payload = _legacy_payload()
    payload["positions"].append({
        "symbol": "EURUSD", "setup": "x", "side": "long", "qty": 1,
        "entry_px": 1.0, "stop_px": None, "target_px": None,
        "opened_at": "2026-05-14T14:00:00+00:00",
        "order_id": "", "stop_order_id": None, "initial_stop_px": None,
        "breakeven_moved": False, "bars_held": 0, "adopted": False,
    })
    json_path = tmp_path / "position_book_test.json"
    json_path.write_text(json.dumps(payload))

    store = _StubStore(existing_count=0)
    imported = store.migrate_legacy_json(json_path, asset_class_for=_ac_for)

    assert imported == 2  # AAPL + BTC/USD; EURUSD skipped
    assert "EURUSD" not in {p.symbol for p, _ in store.opened}
    # File still archived even though one row was skipped
    assert (tmp_path / "position_book_test.json.migrated").exists()


def test_mysql_store_symbol_candidates():
    store = _StubStore()
    # Slash to flat candidate matching
    assert store._get_symbol_candidates("BTC/USD") == ["BTC/USD", "BTCUSD"]
    # Flat to slash candidate matching
    assert "BTC/USD" in store._get_symbol_candidates("BTCUSD")
    assert "BTCUSD" in store._get_symbol_candidates("BTCUSD")
    assert store._get_symbol_candidates("AAPL") == ["AAPL"]

