"""Atomic JSON persistence for the PositionBook.

The trader process holds open positions in memory. Without a snapshot on disk,
a restart leaves the in-memory book empty while the broker still holds the
positions, and the reconciler classifies them as orphans (loud
ADOPTED_CRYPTO_NAKED warnings, lost virtual stops). Writing the book each cycle
and loading it before reconcile keeps engine-managed state across restarts.
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

from state.position_book import OpenPosition, PositionBook


_VERSION = 1


def _pos_to_dict(p: OpenPosition) -> dict:
    return {
        "symbol": p.symbol,
        "setup": p.setup,
        "side": p.side,
        "qty": p.qty,
        "entry_px": p.entry_px,
        "stop_px": p.stop_px,
        "target_px": p.target_px,
        "opened_at": p.opened_at.isoformat(),
        "order_id": p.order_id,
        "breakeven_moved": p.breakeven_moved,
        "bars_held": p.bars_held,
        "stop_order_id": p.stop_order_id,
        "initial_stop_px": p.initial_stop_px,
        "adopted": p.adopted,
    }


def _dict_to_pos(d: dict) -> OpenPosition:
    return OpenPosition(
        symbol=d["symbol"],
        setup=d["setup"],
        side=d["side"],
        qty=float(d["qty"]),
        entry_px=float(d["entry_px"]),
        stop_px=None if d.get("stop_px") is None else float(d["stop_px"]),
        target_px=None if d.get("target_px") is None else float(d["target_px"]),
        opened_at=datetime.fromisoformat(d["opened_at"]),
        order_id=d.get("order_id", ""),
        breakeven_moved=bool(d.get("breakeven_moved", False)),
        bars_held=int(d.get("bars_held", 0)),
        stop_order_id=d.get("stop_order_id"),
        initial_stop_px=(None if d.get("initial_stop_px") is None
                         else float(d["initial_stop_px"])),
        adopted=bool(d.get("adopted", False)),
    )


def write_position_book(path: Path | str, book: PositionBook) -> None:
    """Snapshot the book to `path` atomically (tmp + os.replace).

    The transient just_exited set is intentionally not persisted — it is
    per-cycle state cleared at tick start.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": _VERSION,
        "positions": [_pos_to_dict(p) for p in book.all()],
    }
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2))
    os.replace(tmp, path)


def read_position_book(path: Path | str) -> PositionBook:
    """Load a PositionBook from `path`. Missing file → empty book."""
    path = Path(path)
    book = PositionBook()
    if not path.exists():
        return book
    data = json.loads(path.read_text())
    version = data.get("version")
    if version != _VERSION:
        raise ValueError(
            f"Unsupported position book version: {version!r} (expected {_VERSION})"
        )
    for entry in data.get("positions", []):
        book.add(_dict_to_pos(entry))
    return book
