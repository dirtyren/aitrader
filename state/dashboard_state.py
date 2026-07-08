"""Atomic JSON writer for the live dashboard state."""
from __future__ import annotations
import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


@dataclass
class DashboardSnapshot:
    timestamp: datetime
    equity: float
    day_pnl: float
    symbols: list[dict]
    recent_filter_rejects: list[dict]


def _to_jsonable(o):
    if isinstance(o, datetime):
        return o.isoformat()
    raise TypeError(f"Object of type {type(o).__name__} is not JSON serializable")


def write_dashboard_state(path: Path | str, snap: DashboardSnapshot) -> None:
    """Write the snapshot to `path` atomically (tmp + os.replace)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "timestamp": snap.timestamp.isoformat(),
        "equity": snap.equity,
        "day_pnl": snap.day_pnl,
        "symbols": snap.symbols,
        "recent_filter_rejects": snap.recent_filter_rejects,
    }
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, default=_to_jsonable))
    os.replace(tmp, path)
