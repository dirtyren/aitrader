"""Operator-initiated close of a broker_only reconciliation strike.

Used by the dashboard's Reconciliation tab to flatten an unmanaged broker
position with one click. CLI parity stays in scripts/reconcile_resolve.py
for mysql_only / qty_drift / adopt; this module covers only the
broker_only direction.

The function refuses to act on any other direction. Successful close:
  1. Re-fetch live broker positions to verify the symbol is still held
     (snapshot in the strike row may be stale by minutes).
  2. Cancel any open broker orders for the symbol so brackets/TPs/stops
     don't fight the close we're about to submit.
  3. Submit a market order with a synthetic role=exit COID
     (operator/cleanslate/<SYMBOL>) so the reconciler attributes the fill.
  4. Resolve the strike with reason 'operator_closed_broker_only',
     capturing operator_note in the audit event.
  5. Append a single record to runtime/operator_close_audit_<ts>.jsonl.

If the broker is already flat by the time of the click, no order is
submitted; the strike is resolved with reason 'reconciled_gone_at_close_time'.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from broker.client_order_id import Role, make_client_order_id
from broker.safe_close import submit_close_with_drift_recovery
from state.mysql_store import MySQLStore

logger = logging.getLogger(__name__)

_AUDIT_DIR = "runtime"
_MIN_NOTE_LEN = 3


@dataclass
class CloseResult:
    """Outcome of close_broker_only_strike. Surfaced to the UI."""
    status: str            # 'submitted' | 'already_flat' | 'submit_failed' | 'noop_already_resolved'
    strike_id: int
    symbol: str | None = None
    coid: str | None = None
    alpaca_order_id: str | None = None
    broker_qty: float | None = None
    broker_side: str | None = None
    error: str | None = None
    audit_path: str | None = None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _audit_path() -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return os.path.join(_AUDIT_DIR, f"operator_close_audit_{ts}.jsonl")


def _write_audit(record: dict[str, Any]) -> str:
    path = _audit_path()
    os.makedirs(_AUDIT_DIR, exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(record) + "\n")
    return path


def _alpaca_side_to_close(broker_side: str | None, qty: float) -> str:
    """Resolve broker side -> close order side.

    Prefer the explicit 'long'/'short' string from the live position dict.
    Fallback: positive qty implies long (close = sell), negative implies short.
    """
    if broker_side == "long":
        return "sell"
    if broker_side == "short":
        return "buy"
    return "sell" if qty > 0 else "buy"


def _live_broker_position(alpaca: Any, symbol: str) -> dict | None:
    """Look up a live Alpaca position by symbol. None if not held."""
    positions = alpaca.get_positions()
    for p in positions:
        if p.get("symbol") == symbol:
            return p
    return None


def _cancel_orders_for_symbol(alpaca: Any, symbol: str) -> list[dict]:
    """Cancel all open orders for one symbol. Returns per-order results."""
    try:
        orders = alpaca.list_orders(status="open", symbols=[symbol], nested=False)
    except Exception as exc:
        logger.warning("list_orders failed for %s: %s", symbol, exc)
        return [{"action": "list_open_orders_failed", "error": str(exc)}]
    out: list[dict] = []
    for o in orders:
        oid = o.get("id")
        if not oid:
            continue
        try:
            alpaca.cancel_order(oid)
            out.append({"action": "cancel_order", "alpaca_order_id": oid, "status": "cancelled"})
        except Exception as exc:
            out.append({"action": "cancel_order", "alpaca_order_id": oid,
                        "status": "cancel_failed", "error": str(exc)})
    return out


def close_broker_only_strike(
    *,
    store: MySQLStore,
    alpaca: Any,
    strike_id: int,
    operator_note: str,
) -> CloseResult:
    """Close one broker_only position and resolve its reconciliation strike.

    Raises ValueError on bad inputs (empty note, wrong-direction strike).
    Other failures are reflected in the returned CloseResult.status.
    """
    note = (operator_note or "").strip()
    if len(note) < _MIN_NOTE_LEN:
        raise ValueError(
            f"operator_note must be at least {_MIN_NOTE_LEN} non-whitespace chars"
        )

    strike = store.get_strike_by_id(strike_id)
    if strike is None:
        raise ValueError(f"strike #{strike_id} not found")
    if strike.direction != "broker_only":
        raise ValueError(
            f"strike #{strike_id} has direction={strike.direction!r}, "
            f"only 'broker_only' may be closed via this path"
        )
    if strike.resolved:
        return CloseResult(
            status="noop_already_resolved",
            strike_id=strike_id,
            symbol=strike.symbol,
        )

    symbol = strike.symbol
    audit: dict[str, Any] = {
        "ts": _now_iso(),
        "action": "operator_close_begin",
        "strike_id": strike_id,
        "symbol": symbol,
        "snapshot": strike.last_observed_state,
        "operator_note": note,
    }

    # Re-fetch live broker truth — strike snapshot can be stale by minutes.
    live = _live_broker_position(alpaca, symbol)
    if live is None:
        store.resolve_strike(
            strike_id, reason="reconciled_gone_at_close_time",
            operator_note=note,
        )
        audit["action"] = "operator_close_already_flat"
        path = _write_audit(audit)
        return CloseResult(
            status="already_flat",
            strike_id=strike_id,
            symbol=symbol,
            audit_path=path,
        )

    qty_signed = float(live.get("qty", 0))
    qty_abs = abs(qty_signed)
    broker_side = live.get("side")
    close_side = _alpaca_side_to_close(broker_side, qty_signed)
    coid = make_client_order_id("operator", "cleanslate", symbol, Role.EXIT)

    audit["broker_qty"] = qty_signed
    audit["broker_side"] = broker_side
    audit["close_order_side"] = close_side
    audit["close_order_qty"] = qty_abs
    audit["client_order_id"] = coid

    # Cancel open orders (brackets / TPs / stops) before submitting close.
    cancellations = _cancel_orders_for_symbol(alpaca, symbol)
    audit["cancellations"] = cancellations

    # Submit market close. Crypto fees can drain between snapshot and submit;
    # submit_close_with_drift_recovery shaves a tiny margin and falls back to
    # broker truth on rejection. asset_class is inferred from the live position.
    asset_class = "crypto" if live.get("asset_class") == "crypto" else "equity"
    order = submit_close_with_drift_recovery(
        client=alpaca,
        symbol=symbol,
        qty=qty_abs,
        side=close_side,
        client_order_id=coid,
        asset_class=asset_class,
    )
    if order is None:
        # Strike stays unresolved so operator can retry.
        audit["action"] = "operator_close_submit_failed"
        audit["error"] = "submit_close_with_drift_recovery returned None — see logs"
        path = _write_audit(audit)
        return CloseResult(
            status="submit_failed",
            strike_id=strike_id,
            symbol=symbol,
            coid=coid,
            broker_qty=qty_signed,
            broker_side=broker_side,
            error=audit["error"],
            audit_path=path,
        )

    alpaca_order_id = order.get("id")
    audit["action"] = "operator_close_submitted"
    audit["alpaca_order_id"] = alpaca_order_id

    # Resolve the strike (writes operator_action event with note in payload).
    store.resolve_strike(
        strike_id, reason="operator_closed_broker_only", operator_note=note,
    )

    path = _write_audit(audit)
    return CloseResult(
        status="submitted",
        strike_id=strike_id,
        symbol=symbol,
        coid=coid,
        alpaca_order_id=alpaca_order_id,
        broker_qty=qty_signed,
        broker_side=broker_side,
        audit_path=path,
    )
