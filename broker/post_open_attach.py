"""Post-open OCO attach for pre-market (Gap-and-Go) entries.

The Gap-and-Go strategy submits plain extended-hours limit entries during
pre-market. Server-side bracket orders are not allowed extended_hours, so the
stop/target legs must be attached after the regular session opens.

This module is called once at 09:30:00 ET each session by the Gap-and-Go loop,
before the first regular-session bar evaluation. It walks every position in
the book that carries ``pending_oco_attach=True`` and:

1. Reconciles the position qty against the broker (handles partial fills).
2. Submits an OCO (stop + target) via the Alpaca client.
3. On success: clears ``pending_oco_attach``.
4. On failure: market-closes the position immediately as a failsafe — better
   to flatten than to hold naked stock once the regular session is live.

The function is intentionally idempotent: positions already attached
(``pending_oco_attach=False``) are skipped.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Iterable

from broker.client_order_id import Role, make_client_order_id
from state.position_book import OpenPosition, PositionBook

logger = logging.getLogger(__name__)


def _broker_position_qty(client, symbol: str) -> float | None:
    """Return abs(qty) for ``symbol`` from the broker's positions list, or None."""
    try:
        positions = client.get_positions()
    except Exception as exc:
        logger.error("OCO_ATTACH_GET_POSITIONS_FAILED symbol=%s error=%s",
                     symbol, exc, exc_info=True)
        return None
    sym_norm = symbol.replace("/", "")
    for p in positions:
        broker_sym = (p.get("symbol") or "").replace("/", "")
        if broker_sym == sym_norm:
            try:
                return abs(float(p.get("qty") or 0.0))
            except (TypeError, ValueError):
                return None
    return None


def _exit_side(position_side: str) -> str:
    """Side that closes a long/short position."""
    return "sell" if position_side == "long" else "buy"


def _failsafe_market_close(client, pos: OpenPosition, qty: float,
                           strategy_name: str) -> None:
    """Submit a market order to flatten a position for which OCO attach failed."""
    coid = make_client_order_id(
        strategy_name, pos.setup, pos.symbol, Role.EXIT,
    )
    try:
        client.submit_order(
            symbol=pos.symbol, qty=qty, side=_exit_side(pos.side),
            order_type="market", time_in_force="day",
            client_order_id=coid,
        )
        logger.warning(
            "OCO_ATTACH_FAILSAFE_CLOSED symbol=%s setup=%s qty=%s",
            pos.symbol, pos.setup, qty,
        )
    except Exception as exc:
        logger.error(
            "OCO_ATTACH_FAILSAFE_CLOSE_FAILED symbol=%s setup=%s qty=%s error=%s",
            pos.symbol, pos.setup, qty, exc, exc_info=True,
        )


def attach_brackets_for_premarket_fills(
    book: PositionBook,
    client,
    strategy_name: str,
    now: datetime,
) -> dict:
    """Attach OCO brackets to every position with ``pending_oco_attach=True``.

    Returns a small dict summary suitable for logging:
        {"attached": int, "failsafe_closed": int, "skipped": int}

    ``client`` is the raw Alpaca client (with ``attach_oco``, ``submit_order``,
    and ``get_positions``). It is passed in directly rather than via
    OrderExecutor so this routine can run before the executor's per-cycle
    state is reset.
    """
    pending = [p for p in book.all() if p.pending_oco_attach]
    summary = {"attached": 0, "failsafe_closed": 0, "skipped": 0}
    if not pending:
        return summary

    logger.info(
        "OCO_ATTACH_START ts=%s pending=%d strategy=%s",
        now.isoformat(), len(pending), strategy_name,
    )

    for pos in pending:
        if pos.stop_px is None or pos.target_px is None:
            # Defensive: a Gap-and-Go entry always carries both. If neither
            # exists, the bracket is unattachable; flatten as the safe default.
            logger.error(
                "OCO_ATTACH_MISSING_LEVELS symbol=%s setup=%s stop=%s target=%s",
                pos.symbol, pos.setup, pos.stop_px, pos.target_px,
            )
            _failsafe_market_close(client, pos, pos.qty, strategy_name)
            summary["failsafe_closed"] += 1
            continue

        # Reconcile qty against the broker — pre-market limit orders may have
        # filled partially or not at all.
        broker_qty = _broker_position_qty(client, pos.symbol)
        if broker_qty is None or broker_qty <= 0:
            logger.warning(
                "OCO_ATTACH_NO_BROKER_QTY symbol=%s setup=%s book_qty=%s broker_qty=%s",
                pos.symbol, pos.setup, pos.qty, broker_qty,
            )
            # Nothing on the broker side — clear the flag and let the regular
            # reconciler reconcile the local book on its next cycle.
            pos.pending_oco_attach = False
            summary["skipped"] += 1
            continue

        if broker_qty != pos.qty:
            logger.info(
                "OCO_ATTACH_QTY_RECONCILED symbol=%s setup=%s book_qty=%s broker_qty=%s",
                pos.symbol, pos.setup, pos.qty, broker_qty,
            )
            pos.qty = broker_qty

        oco_coid = make_client_order_id(
            strategy_name, pos.setup, pos.symbol, Role.STOP,
        )
        try:
            order = client.attach_oco(
                symbol=pos.symbol,
                qty=broker_qty,
                side=_exit_side(pos.side),
                stop_price=pos.stop_px,
                target_price=pos.target_px,
                time_in_force="day",
                client_order_id=oco_coid,
            )
        except Exception as exc:
            logger.error(
                "OCO_ATTACH_FAILED symbol=%s setup=%s qty=%s stop=%.4f target=%.4f error=%s",
                pos.symbol, pos.setup, broker_qty,
                pos.stop_px, pos.target_px, exc, exc_info=True,
            )
            _failsafe_market_close(client, pos, broker_qty, strategy_name)
            summary["failsafe_closed"] += 1
            # Do NOT clear pending_oco_attach — leave the marker for a downstream
            # reconciler / operator to inspect why attach failed.
            continue

        pos.pending_oco_attach = False
        # Capture the OCO order id for later cancellation if needed (parity
        # with bracket flow, where stop_order_id is used to PATCH on breakeven).
        pos.stop_order_id = (order.get("id") if isinstance(order, dict) else None)
        summary["attached"] += 1
        logger.info(
            "OCO_ATTACHED symbol=%s setup=%s qty=%s stop=%.4f target=%.4f order_id=%s",
            pos.symbol, pos.setup, broker_qty, pos.stop_px, pos.target_px,
            order.get("id") if isinstance(order, dict) else None,
        )

    logger.info("OCO_ATTACH_DONE %s", summary)
    return summary
