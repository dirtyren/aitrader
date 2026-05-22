import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from state.position_book import OpenPosition, PositionBook
from state.position_book_store import (
    read_position_book,
    write_position_book,
)


def _pos(symbol="AAPL", **overrides) -> OpenPosition:
    base = dict(
        symbol=symbol,
        setup="vwap_wave",
        side="long",
        qty=10.0,
        entry_px=100.0,
        stop_px=95.0,
        target_px=110.0,
        opened_at=datetime(2026, 5, 22, 13, 0, tzinfo=timezone.utc),
        order_id="ord-1",
        breakeven_moved=False,
        bars_held=0,
        stop_order_id="stp-1",
        initial_stop_px=95.0,
        adopted=False,
    )
    base.update(overrides)
    return OpenPosition(**base)


def test_read_returns_empty_book_when_file_missing(tmp_path: Path):
    book = read_position_book(tmp_path / "missing.json")
    assert isinstance(book, PositionBook)
    assert book.count() == 0


def test_round_trip_full_position(tmp_path: Path):
    book = PositionBook()
    book.add(_pos())
    out = tmp_path / "book.json"
    write_position_book(out, book)

    loaded = read_position_book(out)
    assert loaded.count() == 1
    p = loaded.get("AAPL")
    assert p.symbol == "AAPL"
    assert p.setup == "vwap_wave"
    assert p.side == "long"
    assert p.qty == 10.0
    assert p.entry_px == 100.0
    assert p.stop_px == 95.0
    assert p.target_px == 110.0
    assert p.opened_at == datetime(2026, 5, 22, 13, 0, tzinfo=timezone.utc)
    assert p.order_id == "ord-1"
    assert p.breakeven_moved is False
    assert p.bars_held == 0
    assert p.stop_order_id == "stp-1"
    assert p.initial_stop_px == 95.0
    assert p.adopted is False


def test_round_trip_preserves_optional_none_fields(tmp_path: Path):
    book = PositionBook()
    book.add(_pos(
        symbol="BTCUSD",
        setup="adopted",
        stop_px=None,
        target_px=None,
        stop_order_id=None,
        initial_stop_px=None,
        adopted=True,
    ))
    out = tmp_path / "book.json"
    write_position_book(out, book)

    loaded = read_position_book(out)
    p = loaded.get("BTCUSD")
    assert p.stop_px is None
    assert p.target_px is None
    assert p.stop_order_id is None
    assert p.initial_stop_px is None
    assert p.adopted is True


def test_round_trip_preserves_mutable_fields(tmp_path: Path):
    book = PositionBook()
    pos = _pos()
    pos.breakeven_moved = True
    pos.bars_held = 17
    pos.stop_px = 100.0  # moved to breakeven
    book.add(pos)

    out = tmp_path / "book.json"
    write_position_book(out, book)
    loaded = read_position_book(out)
    p = loaded.get("AAPL")
    assert p.breakeven_moved is True
    assert p.bars_held == 17
    assert p.stop_px == 100.0
    assert p.initial_stop_px == 95.0  # original stop preserved


def test_round_trip_multiple_positions(tmp_path: Path):
    book = PositionBook()
    book.add(_pos(symbol="AAPL"))
    book.add(_pos(symbol="MSFT", side="short", qty=5.0, entry_px=200.0,
                  stop_px=205.0, target_px=190.0, initial_stop_px=205.0))
    book.add(_pos(symbol="ETHUSD", setup="adopted", stop_px=None,
                  target_px=None, stop_order_id=None, initial_stop_px=None,
                  adopted=True))

    out = tmp_path / "book.json"
    write_position_book(out, book)
    loaded = read_position_book(out)
    assert loaded.count() == 3
    assert {s for s in loaded.symbols()} == {"AAPL", "MSFT", "ETHUSD"}
    assert loaded.get("MSFT").side == "short"
    assert loaded.get("ETHUSD").adopted is True


def test_write_creates_parent_dir(tmp_path: Path):
    book = PositionBook()
    book.add(_pos())
    out = tmp_path / "nested" / "sub" / "book.json"
    write_position_book(out, book)
    assert out.exists()
    assert read_position_book(out).count() == 1


def test_write_is_atomic_no_lingering_tmp(tmp_path: Path):
    book = PositionBook()
    book.add(_pos())
    out = tmp_path / "book.json"
    write_position_book(out, book)
    write_position_book(out, book)
    assert out.exists()
    assert not (tmp_path / "book.json.tmp").exists()


def test_just_exited_is_not_persisted(tmp_path: Path):
    book = PositionBook()
    book.add(_pos())
    book.close("AAPL")
    assert book.was_just_exited("AAPL")

    out = tmp_path / "book.json"
    write_position_book(out, book)
    loaded = read_position_book(out)
    assert loaded.count() == 0
    assert not loaded.was_just_exited("AAPL")


def test_payload_includes_version(tmp_path: Path):
    book = PositionBook()
    book.add(_pos())
    out = tmp_path / "book.json"
    write_position_book(out, book)
    data = json.loads(out.read_text())
    assert data.get("version") == 1
    assert isinstance(data.get("positions"), list)


def test_read_rejects_unknown_version(tmp_path: Path):
    out = tmp_path / "book.json"
    out.write_text(json.dumps({"version": 999, "positions": []}))
    with pytest.raises(ValueError, match="version"):
        read_position_book(out)


def test_read_empty_book_file(tmp_path: Path):
    out = tmp_path / "book.json"
    out.write_text(json.dumps({"version": 1, "positions": []}))
    loaded = read_position_book(out)
    assert loaded.count() == 0
