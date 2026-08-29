"""Submit a market close that survives broker-vs-book qty drift.

Crypto fees are charged in the asset, so a position whose book qty equals the
filled entry qty drifts below broker truth by the fee amount over the position's
life. Alpaca rejects closes whose qty exceeds available with:

    insufficient balance for PEPE (requested: 25965312924.201588,
                                    available: 25965312924.201586171)

The available value can also drop between `get_positions()` and `submit_order()`
as new fees post, so re-submitting at exact broker qty is itself racy. This
helper applies a small relative safety margin on every submit so the ask is
strictly below available.

The flow:
    1. Submit at min(requested_qty, broker_qty * (1 - margin)).
    2. On insufficient-balance / qty rejection, refresh broker truth and retry
       once at broker_qty * (1 - margin). Any leftover dust is picked up by the
       reconciler's auto-close path (or, if it falls under the dust threshold,
       quietly ignored).

Equity closes are unaffected: equity qty is integer, the margin rounds away,
and the function falls through to a plain submit at the requested qty.
"""
from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)
log_ = log   # alias: `log` is also a keyword arg below

# 1 part in 10^6 — well above crypto fee drift between snapshot and submit
# (~0.1% over a position's lifetime, but only a few microseconds elapse here),
# small enough that we never close materially less than requested.
DEFAULT_DRIFT_MARGIN = 1e-6


def _shrink(qty: float, margin: float, *, asset_class: str) -> float:
    """Apply the fee-drift safety margin.

    Equity fees come off the cash side, so book qty never exceeds broker qty —
    the margin is unnecessary and would shave whole shares off an integer
    request (e.g. int(10 * 0.999999) = 9). Equity passes through unchanged.

    Crypto fees come out of the asset side, so book qty drifts above broker
    qty over a position's life. We shave `margin` off the request to leave
    headroom for any drift accumulated since the snapshot.
    """
    if asset_class == "equity":
        return qty
    return qty * (1.0 - margin)


def _broker_qty_for(client: Any, symbol: str) -> float | None:
    """Return abs broker qty for `symbol`, or None if no position is open.

    Symbol comparison is slash-insensitive so BTC/USD matches BTCUSD.
    """
    try:
        positions = client.get_positions()
    except Exception as exc:
        log.error("SAFE_CLOSE_GET_POSITIONS_FAILED symbol=%s error=%s",
                  symbol, exc, exc_info=True)
        return None
    flat = symbol.replace("/", "")
    for p in positions or []:
        if p.get("symbol", "").replace("/", "") == flat:
            try:
                return abs(float(p.get("qty", 0)))
            except (TypeError, ValueError):
                return None
    return None


def cancel_open_orders_for_symbol(
    client: Any,
    symbol: str,
    *,
    log: Any = None,
    log_prefix: str = "CANCEL_OPEN_ORDERS",
) -> list[str]:
    """Cancel EVERY open order for ``symbol`` on the broker. Best-effort.

    Why this exists as one shared helper rather than "cancel the ids the book
    happens to carry": ``OrderExecutor.submit`` records ``stop_order_id`` from
    the OCO response but sets ``target_order_id = None`` unconditionally, so
    the take-profit leg of an equity OCO is INVISIBLE to the position book.
    A close that only cancels the book's ids leaves that sell-limit live,
    holding the shares, and Alpaca rejects the market close with
    "insufficient qty available for order (requested: N, available: 0)" —
    which silently carries the position past the intended flatten with no
    stop attached.

    Enumerating the broker's own open orders for the symbol is the only
    mechanism that cannot miss a leg the book never recorded. This is the
    same cancel-then-close remediation the reconciler already performs on
    broker-only orphans (``reconciler/main.py``), lifted here so every caller
    shares one implementation.

    Individual cancel failures are tolerated and logged: an already-filled or
    already-cancelled leg raises, and that must never prevent the close.

    Returns the list of order IDs actually cancelled. Never raises.
    """
    logger = log if log is not None else log_
    try:
        open_orders = client.list_orders(
            status="open", symbols=[symbol], nested=False,
        )
    except Exception as exc:
        logger.error(
            "%s_LIST_ORDERS_FAILED symbol=%s error=%s",
            log_prefix, symbol, exc, exc_info=True,
        )
        return []

    if not open_orders:
        return []
    if not isinstance(open_orders, (list, tuple)):
        logger.error(
            "%s_LIST_ORDERS_UNUSABLE symbol=%s type=%s",
            log_prefix, symbol, type(open_orders).__name__,
        )
        return []

    cancelled: list[str] = []
    for order in open_orders:
        # `nested=False` flattens bracket children into the top level — they
        # carry parent_id but otherwise behave like any cancellable order.
        order_id = order.get("id") if isinstance(order, dict) else None
        if not order_id:
            continue
        try:
            client.cancel_order(order_id)
            cancelled.append(order_id)
        except Exception as exc:
            logger.warning(
                "%s_CANCEL_FAILED symbol=%s order_id=%s error=%s",
                log_prefix, symbol, order_id, exc,
            )
    if cancelled:
        logger.info(
            "%s_CANCELLED symbol=%s n=%d order_ids=%s",
            log_prefix, symbol, len(cancelled), cancelled,
        )
    return cancelled


def submit_close_with_drift_recovery(
    *,
    client: Any,
    symbol: str,
    qty: float,
    side: str,
    client_order_id: str,
    asset_class: str = "crypto",
    margin: float = DEFAULT_DRIFT_MARGIN,
    extra_submit_kwargs: dict | None = None,
) -> dict | None:
    """Submit a market close, retrying once at broker truth on qty rejection.

    Args:
        client: an AlpacaClient (or compatible) — needs submit_order, get_positions.
        symbol: Alpaca-form symbol ("BTC/USD" or "AAPL"). Passed back unchanged
            on submit; only flattened internally for position lookup.
        qty: requested close qty (always positive — caller decides side).
        side: Alpaca side ("buy" or "sell").
        client_order_id: COID for the close.
        asset_class: "crypto" applies fractional margin; "equity" floors to int.
        margin: fraction to shave off as fee-drift headroom (default 1e-6).
        extra_submit_kwargs: forwarded to submit_order (e.g. extended_hours).

    Returns the order dict on success, or None if both attempts failed.
    """
    extra_submit_kwargs = extra_submit_kwargs or {}

    # First attempt: requested qty, shrunk by margin so a tiny book/broker
    # drift doesn't trigger Alpaca's insufficient-balance rejection.
    first_qty = _shrink(qty, margin, asset_class=asset_class)
    if first_qty <= 0:
        log.warning(
            "SAFE_CLOSE_NONPOSITIVE_QTY symbol=%s requested=%s shrunk=%s",
            symbol, qty, first_qty,
        )
        return None

    try:
        return client.submit_order(
            symbol=symbol, qty=first_qty, side=side,
            order_type="market", time_in_force="gtc",
            client_order_id=client_order_id,
            **extra_submit_kwargs,
        )
    except Exception as exc:
        msg = str(exc).lower()
        if not _looks_like_qty_rejection(msg):
            log.error("SAFE_CLOSE_FAILED symbol=%s qty=%s error=%s",
                      symbol, first_qty, exc, exc_info=True)
            return None

    # Retry path: pull broker truth, shrink, resubmit. If the broker has
    # nothing or the shrunk qty rounds to zero, give up.
    broker_qty = _broker_qty_for(client, symbol)
    if broker_qty is None or broker_qty <= 0:
        log.warning(
            "SAFE_CLOSE_NO_BROKER_POSITION symbol=%s requested=%s",
            symbol, qty,
        )
        return None

    retry_qty = _shrink(broker_qty, margin, asset_class=asset_class)
    if retry_qty <= 0:
        log.warning(
            "SAFE_CLOSE_DUST symbol=%s broker_qty=%s shrunk=%s — leaving for reconciler",
            symbol, broker_qty, retry_qty,
        )
        return None

    log.warning(
        "SAFE_CLOSE_QTY_RECONCILE symbol=%s requested=%s broker_qty=%s retry_qty=%s",
        symbol, qty, broker_qty, retry_qty,
    )
    try:
        return client.submit_order(
            symbol=symbol, qty=retry_qty, side=side,
            order_type="market", time_in_force="gtc",
            client_order_id=client_order_id,
            **extra_submit_kwargs,
        )
    except Exception as exc:
        log.error(
            "SAFE_CLOSE_RETRY_FAILED symbol=%s retry_qty=%s error=%s",
            symbol, retry_qty, exc, exc_info=True,
        )
        return None


def _looks_like_qty_rejection(msg_lower: str) -> bool:
    """Match the Alpaca error fragments that mean 'qty exceeds available'.

    Crypto: "insufficient balance for <ASSET> (requested: X, available: Y)".
    Equity: "insufficient qty available for order" or "not enough" variants.
    Generic 'qty' is intentionally NOT matched alone — too ambiguous; existing
    callers used it but it causes false-positive recovery on unrelated errors.
    """
    return (
        "insufficient balance" in msg_lower
        or "insufficient qty" in msg_lower
        or "not enough" in msg_lower
    )
