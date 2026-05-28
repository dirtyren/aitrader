"""Persistent state for the reconciler service.

Currently stores only `last_orders_check_ts` — the high-water mark for the
Alpaca orders pull. Atomic write (temp file + rename) so a crash mid-write
never produces a partial JSON.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass
class ReconcilerState:
    last_orders_check_ts: datetime | None = None


def load_state(path: str) -> ReconcilerState:
    """Load state from a JSON file. Missing or corrupt → empty state."""
    p = Path(path)
    if not p.exists():
        return ReconcilerState()
    try:
        data = json.loads(p.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("RECONCILER_STATE_LOAD_FAILED path=%s err=%s", path, exc)
        return ReconcilerState()
    raw = data.get("last_orders_check_ts")
    ts: datetime | None = None
    if raw:
        try:
            ts = datetime.fromisoformat(raw)
        except ValueError:
            log.warning("RECONCILER_STATE_BAD_TS raw=%s", raw)
    return ReconcilerState(last_orders_check_ts=ts)


def save_state(path: str, *, last_orders_check_ts: datetime | None) -> None:
    """Atomically write state to disk."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "last_orders_check_ts": (
            last_orders_check_ts.isoformat() if last_orders_check_ts else None
        ),
    }
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(payload))
    os.replace(tmp, p)
