from __future__ import annotations
from typing import Iterable


def _normalize_asset_class(raw: str | None) -> str | None:
    """Map Alpaca's asset_class strings to the codebase's canonical names.

    "us_equity" -> "equity"
    "crypto"    -> "crypto"
    anything else -> None (caller logs and skips)
    """
    if raw is None:
        return None
    s = raw.strip().lower()
    if s == "us_equity":
        return "equity"
    if s == "crypto":
        return "crypto"
    return None


def _normalize_side(raw: str) -> str:
    """Alpaca position side is 'long' or 'short' — pass through, defended."""
    s = (raw or "").strip().lower()
    if s in ("long", "short"):
        return s
    raise ValueError(f"Unexpected position side: {raw!r}")


def _index_bracket_children(orders: Iterable[dict]) -> dict[str, dict]:
    """Index open bracket children by symbol from a flat list of orders.

    Handles two shapes: (a) parent order with legs nested under `legs`,
    (b) orphaned children appearing as top-level orders with `parent_id` set.

    Returns: {symbol: {"stop": leg_dict | None, "target": leg_dict | None}}
    """
    out: dict[str, dict] = {}

    def _classify(order: dict) -> tuple[str | None, dict] | None:
        otype = (order.get("type") or "").lower()
        symbol = order.get("symbol")
        if symbol is None:
            return None
        if otype in ("stop", "stop_limit"):
            return ("stop", order)
        if otype == "limit" and order.get("limit_price") is not None:
            return ("target", order)
        return None

    for order in orders:
        legs = order.get("legs") or []
        candidates = list(legs) if legs else [order]
        for cand in candidates:
            classified = _classify(cand)
            if classified is None:
                continue
            kind, leg = classified
            symbol = leg["symbol"]
            slot = out.setdefault(symbol, {"stop": None, "target": None})
            if slot[kind] is None:
                slot[kind] = leg

    return out
