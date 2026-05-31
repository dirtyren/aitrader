"""Close every open MySQL position for a strategy via the broker.

Used by the dashboard's strategy-disable action and by the trader's own
disable-sweep on its next cycle. Both paths share this code so MySQL/broker
ordering and COID stamping stay consistent.

Per-row flow:
  1. Cancel any open broker orders for the symbol (bracket children would
     otherwise lock the qty and reject the close).
  2. Submit a market close via submit_close_with_drift_recovery (handles
     crypto fee-drift margin and broker-truth retry).
  3. On submit success: mysql.position_closed() with the exit COID.
  4. On submit failure: leave the MySQL row open. The strategy stays in the
     'disabling' state and the next sweep retries.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from broker.client_order_id import Role, make_client_order_id
from broker.safe_close import submit_close_with_drift_recovery
from state.mysql_store import MySQLStore, PositionRow

log = logging.getLogger(__name__)


@dataclass
class CloseAllResult:
    """Per-symbol outcome of a close-all sweep.

    `closed` and `failed` together cover every row attempted; their sum is
    `total`. A row whose broker submit returned None lands in `failed` with
    a short reason string.
    """
    total: int = 0
    closed: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)


def _cancel_open_orders_for_symbol(alpaca: Any, symbol: str) -> list[str]:
    """Best-effort cancel of every open broker order for `symbol`.

    Mirrors reconciler/main.py:_cancel_open_orders_for_symbol — extracted
    here to avoid the dashboard importing reconciler internals.
    """
    try:
        orders = alpaca.list_orders(
            status="open", symbols=[symbol], nested=False,
        )
    except Exception as exc:
        log.warning(
            "CLOSE_ALL_LIST_ORDERS_FAILED symbol=%s error=%s", symbol, exc,
        )
        return []

    cancelled: list[str] = []
    for order in orders or []:
        oid = order.get("id")
        if not oid:
            continue
        try:
            alpaca.cancel_order(oid)
            cancelled.append(oid)
        except Exception as exc:
            log.warning(
                "CLOSE_ALL_CANCEL_FAILED symbol=%s order_id=%s error=%s",
                symbol, oid, exc,
            )
    return cancelled


def close_all_open_positions(
    *,
    alpaca: Any,
    mysql: MySQLStore,
    strategy_name: str,
    reason: str,
) -> CloseAllResult:
    """Close every open position for a strategy. Idempotent on re-entry.

    Iterates open PositionRow rows for `mysql.strategy_id`. Each successful
    submit advances the corresponding MySQL row to closed and writes a
    TradeRow. Failed submits leave the MySQL row open so a follow-up sweep
    (or the trader's own loop) can retry.
    """
    result = CloseAllResult()

    with Session(mysql._engine) as session:
        rows = session.query(PositionRow).filter(
            PositionRow.strategy_id == mysql.strategy_id,
            PositionRow.status == "open",
        ).all()
        # Capture scalar fields up front — closing detaches the rows.
        snapshots = [
            {
                "id": r.id,
                "symbol": r.symbol,
                "side": r.side,
                "qty": float(r.qty),
                "asset_class": r.asset_class,
                "setup_name": r.setup_name,
            }
            for r in rows
        ]

    result.total = len(snapshots)
    if not snapshots:
        return result

    for snap in snapshots:
        symbol = snap["symbol"]
        side = snap["side"]
        close_side = "sell" if side == "long" else "buy"
        coid = make_client_order_id(
            "operator", "disable", symbol.replace("/", ""), Role.EXIT,
        )

        cancelled = _cancel_open_orders_for_symbol(alpaca, symbol)
        if cancelled:
            log.info(
                "CLOSE_ALL_CANCELLED_OPEN_ORDERS symbol=%s count=%d",
                symbol, len(cancelled),
            )

        order = submit_close_with_drift_recovery(
            client=alpaca,
            symbol=symbol,
            qty=snap["qty"],
            side=close_side,
            client_order_id=coid,
            asset_class=snap["asset_class"],
        )
        if order is None:
            log.error(
                "CLOSE_ALL_SUBMIT_FAILED strategy=%s symbol=%s setup=%s",
                strategy_name, symbol, snap["setup_name"],
            )
            result.failed.append((symbol, "broker submit returned None"))
            continue

        # The fill price isn't synchronously known from a market submit; we
        # record the entry-equivalent reason and let the reconciler back-fill
        # the actual fill price via apply_tagged_fill on the next cycle.
        # Use the position's entry_px as a placeholder — pnl will be corrected
        # by the reconciler when the fill arrives. Practically: pull broker's
        # current_price right now to keep accounting close to truth.
        try:
            broker_pos = next(
                (p for p in alpaca.get_positions()
                 if p.get("symbol", "").replace("/", "") == symbol.replace("/", "")),
                None,
            )
            exit_px = (
                float(broker_pos.get("current_price"))
                if broker_pos and broker_pos.get("current_price")
                else snap["qty"] and 0.0
            )
        except Exception:
            exit_px = 0.0

        try:
            mysql.position_closed(
                symbol=symbol,
                exit_px=exit_px,
                close_reason=reason,
                setup_name=snap["setup_name"],
                exit_client_order_id=coid,
                strategy_id=mysql.strategy_id,
            )
            result.closed.append(symbol)
        except Exception as exc:
            log.error(
                "CLOSE_ALL_MYSQL_CLOSE_FAILED symbol=%s error=%s",
                symbol, exc, exc_info=True,
            )
            result.failed.append((symbol, f"mysql close failed: {exc}"))

    log.info(
        "CLOSE_ALL_DONE strategy=%s total=%d closed=%d failed=%d",
        strategy_name, result.total, len(result.closed), len(result.failed),
    )
    return result
