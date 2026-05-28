"""Read per-strategy `runtime/trading_state_<strategy>.json` files
with safe fallbacks for the live dashboard tab.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional


def _state_path(strategy: str) -> Path:
    return Path("runtime") / f"trading_state_{strategy}.json"


def get_last_price(strategy: str, symbol: str) -> Optional[float]:
    """Return the last known price for `symbol` under `strategy`,
    or None if the file is missing/malformed or the symbol/field is absent.
    """
    path = _state_path(strategy)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    for row in data.get("symbols", []):
        if row.get("symbol") == symbol:
            v = row.get("last_price")
            return float(v) if v is not None else None
    return None
