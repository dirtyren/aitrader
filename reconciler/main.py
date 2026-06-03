"""Reconciler service main loop.

Cycle order (matches spec §2):
    1. Pull broker truth (positions + fills since last_orders_check_ts).
    2. Apply tagged fills to MySQL (entry recovery + exit close, idempotent).
    3. Check the cross-strategy invariant against the post-fill state.
    4. Process anomalies through the strike rule.
    5. Auto-clear strikes whose anomaly disappeared.
    6. Emit a heartbeat event.

In shadow mode: step 2 mutates nothing (`shadow_would_apply_fill` events
are written instead). Steps 3-6 still run for visibility but the strike
rule never advances counts (each cycle is a strike 1 → self-heal pair).
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from broker.alpaca_client import AlpacaClient
from broker.client_order_id import Role, make_client_order_id, parse_client_order_id
from broker.safe_close import DEFAULT_DRIFT_MARGIN, submit_close_with_drift_recovery
from notifications import send_reconcile_alert, send_reconcile_heartbeat_stale
from reconciler.config import ReconcilerConfig
from reconciler.events import emit_event
from reconciler.fills import apply_tagged_fill
from reconciler.invariant import check_invariant
from reconciler.state import load_state, save_state
from reconciler.strikes import auto_clear_resolved, process_anomaly
from state.mysql_store import (
    EventRow, ManualCloseCooldownRow, MySQLStore, PositionRow, StrategyRow,
    StrikeRow,
)

log = logging.getLogger("reconciler")


def _broker_qty_by_symbol(positions: list[dict]) -> dict[str, float]:
    """Map broker symbol → signed qty (positive=long, negative=short).

    Alpaca returns `qty` already signed (e.g. -136 for a 136-share short),
    so we pass it through unchanged. Side-aware so the invariant catches
    long-vs-short divergence between MySQL and broker.
    """
    out: dict[str, float] = {}
    for p in positions:
        sym = p.get("symbol", "").replace("/", "")
        if not sym:
            continue
        try:
            out[sym] = float(p.get("qty", 0))
        except (TypeError, ValueError):
            log.warning("RECONCILER_BAD_BROKER_QTY symbol=%s p=%s", sym, p)
    return out


def _broker_position_by_symbol(positions: list[dict]) -> dict[str, dict]:
    """Same key shape as _broker_qty_by_symbol, but values are the full dict.

    Needed by the auto-close path so it can read the broker's authoritative
    qty (not the snapshot's, which drifts) and infer side from the sign.
    """
    out: dict[str, dict] = {}
    for p in positions:
        sym = p.get("symbol", "").replace("/", "")
        if sym:
            out[sym] = p
    return out


# Defensive backstop against a runaway chunk loop (e.g. if a future bug ever
# returns a near-zero price). Real positions need at most ~10 chunks even at
# multi-million-dollar notionals with a $190k cap.
_MAX_CHUNKS_PER_POSITION = 50


def _broker_price(broker_pos: dict) -> float | None:
    """Pick the per-share price for sizing a close.

    Alpaca's position dict carries `current_price` (live mark) and
    `avg_entry_price`. We prefer the mark — it's what actually drives Alpaca's
    per-order notional check. Falls back to entry price if the mark is
    missing or unparseable. Returns None if neither is usable.
    """
    for field in ("current_price", "avg_entry_price"):
        raw = broker_pos.get(field)
        if raw is None:
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return None


def _cancel_open_orders_for_symbol(alpaca: Any, symbol: str) -> list[str]:
    """Cancel every open order for ``symbol`` on the broker. Best-effort.

    Why: a position whose qty is locked by open bracket children (stop /
    target legs from the original entry) cannot be market-closed — Alpaca
    returns "insufficient qty available for order (requested: N,
    available: 0)". Cancel-then-close is the standard remediation; if any
    individual cancel fails we log and continue (the close itself will
    fail loudly and the next reconciler cycle retries).

    Returns the list of order IDs that were cancelled. Does not raise.
    """
    try:
        open_orders = alpaca.list_orders(
            status="open", symbols=[symbol], nested=False,
        )
    except Exception as exc:
        log.error(
            "AUTO_CLOSE_LIST_ORDERS_FAILED symbol=%s error=%s",
            symbol, exc, exc_info=True,
        )
        return []

    cancelled: list[str] = []
    for order in open_orders or []:
        # `nested=False` flattens bracket children into the top level — they
        # carry parent_id but otherwise behave like any cancellable order.
        order_id = order.get("id")
        if not order_id:
            continue
        try:
            alpaca.cancel_order(order_id)
            cancelled.append(order_id)
        except Exception as exc:
            log.warning(
                "AUTO_CLOSE_CANCEL_FAILED symbol=%s order_id=%s error=%s",
                symbol, order_id, exc,
            )
    return cancelled


def auto_close_broker_only(
    *,
    alpaca: Any,
    store: MySQLStore,
    session: Session,
    broker_positions: dict[str, dict],
    anomalies: list,
    cfg: ReconcilerConfig,
    now: datetime,
    asset_class: str | None = None,
) -> int:
    """Market-close every broker_only anomaly whose strike count >= threshold.

    aitrader cannot enforce position-management invariants on broker-only
    orphans (no MySQL row → no PositionManager, no stop, no target). After
    `cfg.strike_threshold` consecutive observations, the position is
    confirmed unmanaged and we flatten it.

    Alpaca rejects single orders whose notional exceeds $200k. When the
    position notional is over `cfg.auto_close_max_notional_usd`, the close is
    split into N market-order chunks each sized below the cap.

    Idempotent: if any chunk fails, the strike stays unresolved and the next
    cycle retries against fresh broker truth (Alpaca will report whatever
    remains). If every chunk submits, the position drains over the next few
    seconds and `auto_clear_resolved` marks the strike `self_healed`.

    Returns the number of close orders submitted this cycle (sum across all
    chunks across all anomalies).
    """
    if cfg.shadow_mode:
        return 0

    submitted = 0
    for a in anomalies:
        if a.direction != "broker_only":
            continue
        existing = session.query(StrikeRow).filter(
            StrikeRow.key == a.key,
            StrikeRow.resolved == False,  # noqa: E712 — SQLAlchemy idiom
        ).one_or_none()
        if existing is None or existing.strike_count < cfg.strike_threshold:
            continue

        broker_pos = broker_positions.get(a.symbol)
        if broker_pos is None:
            # Vanished between snapshot and now — let auto_clear_resolved
            # pick it up next cycle.
            continue
        try:
            broker_qty = float(broker_pos.get("qty", 0))
        except (TypeError, ValueError):
            log.error("AUTO_CLOSE_BAD_QTY symbol=%s p=%s", a.symbol, broker_pos)
            continue
        if broker_qty == 0:
            continue

        # Crypto symbols come back from Alpaca already in slash-form; stocks
        # are flat. Use the broker's own symbol so the close matches what we
        # observed.
        broker_symbol = broker_pos.get("symbol") or a.symbol
        side = "sell" if broker_qty > 0 else "buy"
        total_qty = abs(broker_qty)

        price = _broker_price(broker_pos)
        if price is None:
            # No price means we can't safely size a chunk against the $200k
            # cap. Bail loudly rather than send a single oversized order.
            log.error(
                "AUTO_CLOSE_NO_PRICE symbol=%s qty=%s — leaving strike unresolved",
                broker_symbol, total_qty,
            )
            emit_event(
                session,
                type="auto_close_broker_only_failed",
                symbol=a.symbol,
                asset_class=asset_class,
                payload={
                    "broker_symbol": broker_symbol,
                    "side": side, "qty": total_qty,
                    "strike_count": existing.strike_count,
                    "error": "no usable price field on broker position",
                },
            )
            continue

        # Dust check: a position whose notional is below the dust threshold
        # is too small for Alpaca to accept (the broker enforces a per-asset
        # minimum qty, and the prior chunked-close path can leave 1e-9
        # remainders that re-trigger the auto-close on every cycle). Treat
        # such positions as effectively flat: resolve the strike, emit an
        # event, skip the submit. If a fresh entry later pushes the position
        # above the threshold, the normal flow resumes next cycle.
        notional = total_qty * price
        if notional < cfg.auto_close_dust_usd:
            log.warning(
                "AUTO_CLOSE_DUST symbol=%s qty=%s price=%.6f notional=%.6f "
                "threshold=%.2f — resolving strike without submit",
                broker_symbol, total_qty, price, notional,
                cfg.auto_close_dust_usd,
            )
            emit_event(
                session,
                type="auto_close_dust",
                symbol=a.symbol,
                asset_class=asset_class,
                payload={
                    "broker_symbol": broker_symbol,
                    "total_qty": total_qty,
                    "price": price,
                    "notional": notional,
                    "dust_threshold_usd": cfg.auto_close_dust_usd,
                    "strike_count": existing.strike_count,
                },
            )
            existing.resolved = True
            existing.resolved_at = now
            existing.resolved_reason = "auto_close_dust"
            continue

        max_qty_per_chunk = cfg.auto_close_max_notional_usd / price
        chunks = _split_qty(total_qty, max_qty_per_chunk)
        if len(chunks) > _MAX_CHUNKS_PER_POSITION:
            # Defensive: refuse to fan out past a sane limit. Indicates either
            # a bad price or an unrealistic position; let an operator look.
            log.error(
                "AUTO_CLOSE_TOO_MANY_CHUNKS symbol=%s qty=%s price=%.6f chunks=%d cap=%d",
                broker_symbol, total_qty, price, len(chunks),
                _MAX_CHUNKS_PER_POSITION,
            )
            emit_event(
                session,
                type="auto_close_broker_only_failed",
                symbol=a.symbol,
                asset_class=asset_class,
                payload={
                    "broker_symbol": broker_symbol,
                    "side": side, "qty": total_qty,
                    "price": price,
                    "would_be_chunks": len(chunks),
                    "max_chunks": _MAX_CHUNKS_PER_POSITION,
                    "strike_count": existing.strike_count,
                    "error": "too many chunks — operator review required",
                },
            )
            continue

        # Cancel any open orders on this symbol before submitting the close.
        # Stale bracket children (stop / target legs) from the original entry
        # lock the broker qty: Alpaca returns "insufficient qty available
        # for order (requested: N, available: 0)" until those are gone.
        # Best-effort — individual cancel failures are logged and the close
        # proceeds anyway; if the qty is still locked, the close itself
        # fails and we retry next cycle.
        cancelled_ids = _cancel_open_orders_for_symbol(alpaca, broker_symbol)
        if cancelled_ids:
            log.info(
                "AUTO_CLOSE_CANCELLED_OPEN_ORDERS symbol=%s count=%d ids=%s",
                broker_symbol, len(cancelled_ids), cancelled_ids,
            )

        # Crypto fees drain from the asset side between snapshot and submit,
        # so submitting at exact broker_qty races the next fee post and
        # triggers "insufficient balance for <ASSET>". Shave the fee-drift
        # margin off every chunk for crypto. Equity has no such drift.
        is_crypto = broker_pos.get("asset_class") == "crypto"
        chunk_margin = DEFAULT_DRIFT_MARGIN if is_crypto else 0.0

        all_ok = True
        order_ids: list[str | None] = []
        for chunk_qty in chunks:
            submit_qty = chunk_qty * (1.0 - chunk_margin)
            try:
                order = alpaca.submit_order(
                    symbol=broker_symbol, qty=submit_qty, side=side,
                    order_type="market", time_in_force="gtc",
                )
            except Exception as exc:
                log.error(
                    "AUTO_CLOSE_FAILED symbol=%s side=%s qty=%s chunk=%d/%d error=%s",
                    broker_symbol, side, chunk_qty,
                    len(order_ids) + 1, len(chunks), exc, exc_info=True,
                )
                emit_event(
                    session,
                    type="auto_close_broker_only_failed",
                    symbol=a.symbol,
                    asset_class=asset_class,
                    payload={
                        "broker_symbol": broker_symbol,
                        "side": side, "qty": chunk_qty,
                        "chunk_index": len(order_ids) + 1,
                        "total_chunks": len(chunks),
                        "submitted_so_far": order_ids,
                        "strike_count": existing.strike_count,
                        "error": str(exc),
                    },
                )
                all_ok = False
                break

            submitted += 1
            order_id = order.get("id") if isinstance(order, dict) else None
            order_ids.append(order_id)
            log.warning(
                "AUTO_CLOSE_SUBMITTED symbol=%s side=%s qty=%s order_id=%s "
                "chunk=%d/%d strike_count=%d",
                broker_symbol, side, chunk_qty, order_id,
                len(order_ids), len(chunks), existing.strike_count,
            )

        if not all_ok:
            # Partial submission — strike stays unresolved so next cycle
            # retries against whatever's still on the broker.
            continue

        emit_event(
            session,
            type="auto_close_broker_only",
            symbol=a.symbol,
            payload={
                "broker_symbol": broker_symbol,
                "side": side,
                "total_qty": total_qty,
                "price": price,
                "chunks": len(chunks),
                "order_ids": order_ids,
                "strike_count": existing.strike_count,
            },
        )
        existing.resolved = True
        existing.resolved_at = now
        existing.resolved_reason = "auto_closed_broker_only"

    return submitted


_EXIT_ROLES = frozenset({"exit", "stop", "target"})


def _open_rows_for_symbol(
    session: Session, store: MySQLStore, symbol: str,
) -> list[PositionRow]:
    """All open PositionRow rows for a symbol across every strategy.

    Slash-insensitive (BTC/USD vs BTCUSD) via store._get_symbol_candidates.
    """
    candidates = store._get_symbol_candidates(symbol)
    return session.query(PositionRow).filter(
        PositionRow.symbol.in_(candidates),
        PositionRow.status == "open",
    ).all()


def _attribute_qty_drift(
    session: Session,
    store: MySQLStore,
    symbol: str,
    recent_fills: list[dict],
) -> PositionRow | None:
    """Pick the open MySQL row to blame for a qty_drift on `symbol`.

    Trivial case: exactly one open row → that's the row.
    Multi-strategy: walk this cycle's recent_fills, parse each COID, count
    exit-role fills (exit/stop/target) whose (strategy, setup) maps to one
    of the open rows. Return that row only when a single open row gets
    matched. Anything ambiguous returns None — caller emits an event and
    leaves the strike for an operator.
    """
    rows = _open_rows_for_symbol(session, store, symbol)
    if not rows:
        return None
    if len(rows) == 1:
        return rows[0]

    flat = symbol.replace("/", "")
    matched_row_ids: set[int] = set()
    for fill in recent_fills:
        fill_sym = (fill.get("symbol") or "").replace("/", "")
        if fill_sym != flat:
            continue
        parsed = parse_client_order_id(fill.get("client_order_id"))
        if parsed is None or parsed["role"] not in _EXIT_ROLES:
            continue
        strow = session.query(StrategyRow).filter(
            StrategyRow.name == parsed["strategy"],
        ).one_or_none()
        if strow is None:
            continue
        for r in rows:
            if r.strategy_id == strow.id and r.setup_name == parsed["setup"]:
                matched_row_ids.add(r.id)
                break

    if len(matched_row_ids) == 1:
        target_id = next(iter(matched_row_ids))
        for r in rows:
            if r.id == target_id:
                return r
    return None


def _close_unattributed_surplus(
    *,
    alpaca: Any,
    session: Session,
    broker_pos: dict,
    symbol: str,
    surplus_qty: float,
    broker_qty_signed: float,
    open_rows: list[PositionRow],
    strike: StrikeRow,
    cfg: ReconcilerConfig,
    now: datetime,
    asset_class: str | None,
) -> int:
    """Flatten broker surplus that cannot be attributed to any MySQL row.

    Used when a qty_drift anomaly's COID-based attribution is ambiguous
    *and* the broker side grossly exceeds the sum of |qty| across all open
    MySQL rows on the symbol. The surplus is leaked broker-only inventory
    (typical cause: multiple strategies on the same ticker each firing
    their own entry while the aggregate book had no guard). MySQL stays
    untouched — only the surplus closes — and the strike resolves with
    reason ``auto_close_qty_drift_surplus``.

    Returns the number of broker close orders submitted.
    """
    if cfg.shadow_mode:
        return 0

    broker_symbol = broker_pos.get("symbol") or symbol
    side = "sell" if broker_qty_signed > 0 else "buy"

    price = _broker_price(broker_pos)
    if price is None:
        log.error(
            "QTY_DRIFT_SURPLUS_NO_PRICE symbol=%s qty=%s — leaving strike unresolved",
            broker_symbol, surplus_qty,
        )
        emit_event(
            session,
            type="auto_close_qty_drift_surplus_failed",
            symbol=symbol,
            asset_class=asset_class,
            payload={
                "broker_symbol": broker_symbol,
                "side": side, "surplus_qty": surplus_qty,
                "strike_count": strike.strike_count,
                "error": "no usable price field on broker position",
            },
        )
        return 0

    notional = surplus_qty * price
    if notional < cfg.auto_close_dust_usd:
        log.warning(
            "QTY_DRIFT_SURPLUS_DUST symbol=%s qty=%s price=%.6f notional=%.6f "
            "— resolving strike without submit",
            broker_symbol, surplus_qty, price, notional,
        )
        emit_event(
            session,
            type="auto_close_dust",
            symbol=symbol,
            asset_class=asset_class,
            payload={
                "direction": "qty_drift_surplus",
                "broker_symbol": broker_symbol,
                "surplus_qty": surplus_qty, "price": price,
                "notional": notional,
                "dust_threshold_usd": cfg.auto_close_dust_usd,
                "strike_count": strike.strike_count,
            },
        )
        strike.resolved = True
        strike.resolved_at = now
        strike.resolved_reason = "auto_close_dust"
        return 0

    max_qty_per_chunk = cfg.auto_close_max_notional_usd / price
    chunks = _split_qty(surplus_qty, max_qty_per_chunk)
    if len(chunks) > _MAX_CHUNKS_PER_POSITION:
        log.error(
            "QTY_DRIFT_SURPLUS_TOO_MANY_CHUNKS symbol=%s qty=%s chunks=%d",
            broker_symbol, surplus_qty, len(chunks),
        )
        emit_event(
            session,
            type="auto_close_qty_drift_surplus_failed",
            symbol=symbol,
            asset_class=asset_class,
            payload={
                "broker_symbol": broker_symbol,
                "side": side, "surplus_qty": surplus_qty, "price": price,
                "would_be_chunks": len(chunks),
                "max_chunks": _MAX_CHUNKS_PER_POSITION,
                "strike_count": strike.strike_count,
                "error": "too many chunks — operator review required",
            },
        )
        return 0

    cancelled_ids = _cancel_open_orders_for_symbol(alpaca, broker_symbol)
    if cancelled_ids:
        log.info(
            "QTY_DRIFT_SURPLUS_CANCELLED_OPEN_ORDERS symbol=%s count=%d",
            broker_symbol, len(cancelled_ids),
        )

    is_crypto = broker_pos.get("asset_class") == "crypto"
    chunk_margin = DEFAULT_DRIFT_MARGIN if is_crypto else 0.0

    submitted = 0
    order_ids: list[str | None] = []
    for chunk_qty in chunks:
        submit_qty = chunk_qty * (1.0 - chunk_margin)
        coid = make_client_order_id(
            "reconciler", "qty_drift_surplus", symbol, Role.EXIT,
        )
        order = submit_close_with_drift_recovery(
            client=alpaca,
            symbol=broker_symbol,
            qty=submit_qty,
            side=side,
            client_order_id=coid,
            asset_class=("crypto" if is_crypto else "equity"),
        )
        if order is None:
            log.error(
                "QTY_DRIFT_SURPLUS_SUBMIT_FAILED symbol=%s chunk=%d/%d",
                broker_symbol, len(order_ids) + 1, len(chunks),
            )
            emit_event(
                session,
                type="auto_close_qty_drift_surplus_failed",
                symbol=symbol,
                asset_class=asset_class,
                payload={
                    "broker_symbol": broker_symbol,
                    "side": side, "qty": submit_qty,
                    "chunk_index": len(order_ids) + 1,
                    "total_chunks": len(chunks),
                    "submitted_so_far": order_ids,
                    "strike_count": strike.strike_count,
                    "error": "submit returned None — see safe_close logs",
                },
            )
            return submitted
        submitted += 1
        order_ids.append(order.get("id") if isinstance(order, dict) else None)
        log.warning(
            "QTY_DRIFT_SURPLUS_SUBMITTED symbol=%s side=%s qty=%s order_id=%s "
            "chunk=%d/%d strike_count=%d",
            broker_symbol, side, submit_qty, order_ids[-1],
            len(order_ids), len(chunks), strike.strike_count,
        )

    emit_event(
        session,
        type="auto_close_qty_drift_surplus",
        symbol=symbol,
        asset_class=asset_class,
        payload={
            "broker_symbol": broker_symbol,
            "side": side,
            "surplus_qty": surplus_qty,
            "broker_qty": abs(broker_qty_signed),
            "mysql_open_rows": [
                {"strategy_id": r.strategy_id, "setup": r.setup_name,
                 "qty": float(r.qty)}
                for r in open_rows
            ],
            "price": price,
            "chunks": len(chunks),
            "order_ids": order_ids,
            "strike_count": strike.strike_count,
        },
    )
    strike.resolved = True
    strike.resolved_at = now
    strike.resolved_reason = "auto_close_qty_drift_surplus"
    return submitted


def auto_resolve_qty_drift(
    *,
    alpaca: Any,
    store: MySQLStore,
    session: Session,
    broker_positions: dict[str, dict],
    anomalies: list,
    recent_fills: list[dict],
    cfg: ReconcilerConfig,
    now: datetime,
    asset_class: str | None = None,
) -> int:
    """Reconcile qty_drift anomalies whose strike count >= threshold.

    Two cases per drift:
      - broker < |MySQL|  (book overstated): submit a close for the broker's
        full qty, then close the attributed MySQL row entirely. The strategy
        loses its book position; broker is the source of truth.
      - broker > |MySQL|  (broker surplus): submit a close for the surplus
        only. MySQL row stays open with its current qty.

    Attribution is COID-driven via _attribute_qty_drift; ambiguous cases emit
    qty_drift_ambiguous_attribution and leave the strike unresolved.

    Returns the number of broker close orders submitted this cycle.
    """
    if cfg.shadow_mode:
        return 0

    submitted = 0
    for a in anomalies:
        if a.direction != "qty_drift":
            continue
        existing = session.query(StrikeRow).filter(
            StrikeRow.key == a.key,
            StrikeRow.resolved == False,  # noqa: E712 — SQLAlchemy idiom
        ).one_or_none()
        if existing is None or existing.strike_count < cfg.strike_threshold:
            continue

        broker_pos = broker_positions.get(a.symbol)
        if broker_pos is None:
            continue
        try:
            broker_qty_signed = float(broker_pos.get("qty", 0))
        except (TypeError, ValueError):
            log.error("QTY_DRIFT_BAD_QTY symbol=%s p=%s", a.symbol, broker_pos)
            continue
        broker_qty = abs(broker_qty_signed)

        target = _attribute_qty_drift(session, store, a.symbol, recent_fills)
        if target is None:
            open_rows = _open_rows_for_symbol(session, store, a.symbol)
            mysql_sum_abs = sum(abs(float(r.qty)) for r in open_rows)
            surplus = broker_qty - mysql_sum_abs
            # If the broker is holding noticeably more than every open MySQL
            # row combined could justify, the position cannot belong solely to
            # those rows — it's leaked entries (e.g. cross-strategy duplicate
            # signals on the same symbol). We can't credit any single strategy
            # with the surplus, so we don't touch any MySQL row; we just
            # flatten the unattributable excess and resolve the strike. The
            # remaining `mysql_sum_abs` worth of broker qty stays put,
            # matching the aggregate book.
            if surplus > cfg.qty_eps:
                submitted += _close_unattributed_surplus(
                    alpaca=alpaca, session=session,
                    broker_pos=broker_pos, symbol=a.symbol,
                    surplus_qty=surplus, broker_qty_signed=broker_qty_signed,
                    open_rows=open_rows, strike=existing,
                    cfg=cfg, now=now, asset_class=asset_class,
                )
                continue
            emit_event(
                session,
                type="qty_drift_ambiguous_attribution",
                symbol=a.symbol,
                asset_class=asset_class,
                payload={
                    "snapshot": a.snapshot,
                    "open_rows": [
                        {"strategy_id": r.strategy_id, "setup": r.setup_name,
                         "qty": float(r.qty)}
                        for r in open_rows
                    ],
                    "recent_fills_count": len(recent_fills),
                    "strike_count": existing.strike_count,
                },
            )
            continue

        mysql_qty = float(target.qty)
        # broker_only is handled elsewhere; here broker_qty > 0 by construction
        # (qty_drift requires both sides nonzero).
        full_close = broker_qty <= mysql_qty
        close_qty = broker_qty if full_close else (broker_qty - mysql_qty)

        broker_symbol = broker_pos.get("symbol") or a.symbol
        # Side: book is long → close = sell broker qty; book is short → buy.
        # Attribution row tells us which side we manage. broker_qty_signed sign
        # matches book side under qty_drift, so use that for direction.
        side = "sell" if broker_qty_signed > 0 else "buy"
        asset_class = (
            "crypto" if broker_pos.get("asset_class") == "crypto" else "equity"
        )

        price = _broker_price(broker_pos)
        if price is None:
            log.error(
                "QTY_DRIFT_NO_PRICE symbol=%s qty=%s — leaving strike unresolved",
                broker_symbol, close_qty,
            )
            emit_event(
                session,
                type="auto_close_qty_drift_failed",
                strategy_id=target.strategy_id,
                symbol=a.symbol,
                asset_class=asset_class,
                payload={
                    "broker_symbol": broker_symbol,
                    "setup": target.setup_name,
                    "side": side, "close_qty": close_qty,
                    "strike_count": existing.strike_count,
                    "error": "no usable price field on broker position",
                },
            )
            continue

        notional = close_qty * price
        if notional < cfg.auto_close_dust_usd:
            log.warning(
                "QTY_DRIFT_DUST symbol=%s close_qty=%s notional=%.6f — resolving",
                broker_symbol, close_qty, notional,
            )
            emit_event(
                session,
                type="auto_close_dust",
                strategy_id=target.strategy_id,
                symbol=a.symbol,
                asset_class=asset_class,
                payload={
                    "direction": "qty_drift",
                    "broker_symbol": broker_symbol,
                    "close_qty": close_qty, "price": price,
                    "notional": notional,
                    "dust_threshold_usd": cfg.auto_close_dust_usd,
                    "strike_count": existing.strike_count,
                },
            )
            existing.resolved = True
            existing.resolved_at = now
            existing.resolved_reason = "auto_close_dust"
            continue

        # Cancel stale bracket children before close — same reason as
        # auto_close_broker_only: locked broker qty rejects with "insufficient
        # qty available for order".
        cancelled_ids = _cancel_open_orders_for_symbol(alpaca, broker_symbol)
        if cancelled_ids:
            log.info(
                "QTY_DRIFT_CANCELLED_OPEN_ORDERS symbol=%s count=%d",
                broker_symbol, len(cancelled_ids),
            )

        max_qty_per_chunk = cfg.auto_close_max_notional_usd / price
        chunks = _split_qty(close_qty, max_qty_per_chunk)
        if len(chunks) > _MAX_CHUNKS_PER_POSITION:
            log.error(
                "QTY_DRIFT_TOO_MANY_CHUNKS symbol=%s qty=%s chunks=%d",
                broker_symbol, close_qty, len(chunks),
            )
            emit_event(
                session,
                type="auto_close_qty_drift_failed",
                strategy_id=target.strategy_id,
                symbol=a.symbol,
                asset_class=asset_class,
                payload={
                    "broker_symbol": broker_symbol,
                    "setup": target.setup_name,
                    "side": side, "close_qty": close_qty, "price": price,
                    "would_be_chunks": len(chunks),
                    "max_chunks": _MAX_CHUNKS_PER_POSITION,
                    "strike_count": existing.strike_count,
                    "error": "too many chunks — operator review required",
                },
            )
            continue

        all_ok = True
        order_ids: list[str | None] = []
        for chunk_qty in chunks:
            coid = make_client_order_id(
                "reconciler", "qty_drift", a.symbol, Role.EXIT,
            )
            order = submit_close_with_drift_recovery(
                client=alpaca,
                symbol=broker_symbol,
                qty=chunk_qty,
                side=side,
                client_order_id=coid,
                asset_class=asset_class,
            )
            if order is None:
                log.error(
                    "QTY_DRIFT_SUBMIT_FAILED symbol=%s chunk=%d/%d",
                    broker_symbol, len(order_ids) + 1, len(chunks),
                )
                emit_event(
                    session,
                    type="auto_close_qty_drift_failed",
                    strategy_id=target.strategy_id,
                    symbol=a.symbol,
                    asset_class=asset_class,
                    payload={
                        "broker_symbol": broker_symbol,
                        "setup": target.setup_name,
                        "side": side, "qty": chunk_qty,
                        "chunk_index": len(order_ids) + 1,
                        "total_chunks": len(chunks),
                        "submitted_so_far": order_ids,
                        "strike_count": existing.strike_count,
                        "error": "submit returned None — see safe_close logs",
                    },
                )
                all_ok = False
                break
            submitted += 1
            order_ids.append(order.get("id") if isinstance(order, dict) else None)

        if not all_ok:
            continue

        # Trim MySQL only on the full-close branch. Surplus closes leave the
        # row untouched — the broker side now matches book qty.
        if full_close:
            exit_coid = make_client_order_id(
                "reconciler", "qty_drift", a.symbol, Role.EXIT,
            )
            store.position_closed(
                symbol=a.symbol,
                exit_px=price,
                close_reason="auto_resolved_qty_drift",
                setup_name=target.setup_name,
                exit_client_order_id=exit_coid,
                strategy_id=target.strategy_id,
            )

        emit_event(
            session,
            type="auto_close_qty_drift",
            strategy_id=target.strategy_id,
            symbol=a.symbol,
            payload={
                "broker_symbol": broker_symbol,
                "setup": target.setup_name,
                "side": side,
                "mysql_qty": mysql_qty,
                "broker_qty": broker_qty,
                "close_qty": close_qty,
                "full_close": full_close,
                "chunks": len(chunks),
                "order_ids": order_ids,
                "strike_count": existing.strike_count,
            },
        )
        existing.resolved = True
        existing.resolved_at = now
        existing.resolved_reason = "auto_resolved_qty_drift"

    return submitted


_UNFILLED_ORDER_STATUSES = frozenset({
    "new", "accepted", "held", "pending_new", "accepted_for_bidding",
})


def auto_resolve_mysql_only_entry_never_filled(
    *,
    alpaca: Any,
    store: MySQLStore,
    session: Session,
    anomalies: list,
    recent_fills: list[dict],
    cfg: ReconcilerConfig,
    now: datetime,
    asset_class: str | None = None,
) -> int:
    """Resolve mysql_only strikes whose entry order is still unfilled.

    Why this exists: order_executor.py writes the MySQL position row at
    submit time (optimistic insert), so a limit-bracket whose parent
    never fills leaves a phantom open row in MySQL. The reconciler
    correctly raises mysql_only on it; without an auto-resolve path the
    operator has to run scripts/reconcile_resolve.py force-zero every
    time. This path closes only the safe sub-case — entry order still
    sitting at the broker in new/accepted/held with zero filled qty —
    and bails on any sign of an actual fill.

    Returns the number of strikes auto-resolved this cycle.
    """
    if cfg.shadow_mode:
        return 0

    fills_by_coid = {
        f.get("client_order_id"): f
        for f in (recent_fills or [])
        if f.get("client_order_id")
    }

    resolved_count = 0
    for a in anomalies:
        if a.direction != "mysql_only":
            continue
        existing = session.query(StrikeRow).filter(
            StrikeRow.key == a.key,
            StrikeRow.resolved == False,  # noqa: E712 — SQLAlchemy idiom
        ).one_or_none()
        if existing is None or existing.strike_count < cfg.strike_threshold:
            continue

        # 1. Pull the open MySQL row(s) for this (strategy, symbol).
        candidates = store._get_symbol_candidates(a.symbol)
        rows = session.query(PositionRow).filter(
            PositionRow.strategy_id == a.strategy_id,
            PositionRow.symbol.in_(candidates),
            PositionRow.status == "open",
        ).all()
        if not rows:
            # Race: row already closed between invariant check and now.
            # Let auto_clear_resolved pick up the strike next cycle.
            continue
        if len(rows) > 1:
            emit_event(
                session,
                type="mysql_only_ambiguous_setup",
                strategy_id=a.strategy_id,
                symbol=a.symbol,
                asset_class=asset_class,
                payload={
                    "open_rows": [
                        {"setup": r.setup_name, "qty": float(r.qty),
                         "client_order_id": r.client_order_id}
                        for r in rows
                    ],
                    "strike_count": existing.strike_count,
                },
            )
            continue

        row = rows[0]
        entry_coid = row.client_order_id
        setup = row.setup_name
        if not entry_coid:
            # Pre-COID legacy row — operator must reconcile.
            emit_event(
                session,
                type="mysql_only_no_entry_coid",
                strategy_id=a.strategy_id,
                symbol=a.symbol,
                asset_class=asset_class,
                payload={"setup": setup, "qty": float(row.qty),
                         "strike_count": existing.strike_count},
            )
            continue

        # 2. If a fill on this COID is in the same cycle's recent_fills,
        # apply_tagged_fill will close the row in this same transaction —
        # do nothing.
        if entry_coid in fills_by_coid:
            continue

        # 3. Look up the entry order at the broker.
        try:
            open_orders = alpaca.list_orders(
                status="open", symbols=[a.symbol], nested=False,
            )
        except Exception as exc:
            log.error(
                "MYSQL_ONLY_LIST_ORDERS_FAILED symbol=%s error=%s",
                a.symbol, exc, exc_info=True,
            )
            continue

        entry_order = next(
            (o for o in open_orders or []
             if o.get("client_order_id") == entry_coid),
            None,
        )
        if entry_order is None:
            # Not in open orders. Check closed orders for the same COID
            # in case Alpaca actually filled it but our last cycle's
            # `after` window missed the timestamp.
            try:
                closed_orders = alpaca.list_orders(
                    status="closed", symbols=[a.symbol], nested=False,
                )
            except Exception as exc:
                log.error(
                    "MYSQL_ONLY_CLOSED_LOOKUP_FAILED symbol=%s error=%s",
                    a.symbol, exc, exc_info=True,
                )
                continue
            closed_match = next(
                (o for o in closed_orders or []
                 if o.get("client_order_id") == entry_coid),
                None,
            )
            if closed_match is None:
                emit_event(
                    session,
                    type="mysql_only_entry_coid_missing",
                    strategy_id=a.strategy_id,
                    symbol=a.symbol,
                    asset_class=asset_class,
                    payload={"entry_coid": entry_coid, "setup": setup,
                             "qty": float(row.qty),
                             "strike_count": existing.strike_count},
                )
            else:
                emit_event(
                    session,
                    type="mysql_only_filled_at_broker",
                    strategy_id=a.strategy_id,
                    symbol=a.symbol,
                    asset_class=asset_class,
                    payload={
                        "entry_coid": entry_coid, "setup": setup,
                        "alpaca_status": closed_match.get("status"),
                        "filled_qty": closed_match.get("filled_qty"),
                        "strike_count": existing.strike_count,
                    },
                )
            continue

        # 4. Defensive: never auto-cancel an order that has any filled qty.
        try:
            filled_qty = float(entry_order.get("filled_qty") or 0)
        except (TypeError, ValueError):
            filled_qty = 0.0
        order_status = entry_order.get("status", "")
        if filled_qty > 0 or order_status not in _UNFILLED_ORDER_STATUSES:
            emit_event(
                session,
                type="mysql_only_partially_filled",
                strategy_id=a.strategy_id,
                symbol=a.symbol,
                asset_class=asset_class,
                payload={
                    "entry_coid": entry_coid, "setup": setup,
                    "alpaca_status": order_status,
                    "filled_qty": filled_qty,
                    "strike_count": existing.strike_count,
                },
            )
            continue

        # 5. Cancel the entry. Bracket children cascade automatically.
        entry_order_id = entry_order.get("id")
        try:
            alpaca.cancel_order(entry_order_id)
        except Exception as exc:
            # Most likely the order flipped to filled between step 4 and
            # the cancel. Leave the strike unresolved; next cycle's
            # invariant + apply_tagged_fill will resolve it the right way.
            log.warning(
                "MYSQL_ONLY_CANCEL_FAILED symbol=%s order_id=%s error=%s",
                a.symbol, entry_order_id, exc,
            )
            emit_event(
                session,
                type="mysql_only_cancel_failed",
                strategy_id=a.strategy_id,
                symbol=a.symbol,
                asset_class=asset_class,
                payload={
                    "entry_coid": entry_coid, "entry_order_id": entry_order_id,
                    "error": str(exc),
                    "strike_count": existing.strike_count,
                },
            )
            continue

        # 6. Close the MySQL row at entry price (zero PnL — the position
        # never actually existed at the broker). Archives a TradeRow with
        # close_reason='entry_never_filled' for audit.
        entry_px = float(row.entry_px)
        store.position_closed(
            symbol=a.symbol,
            exit_px=entry_px,
            close_reason="entry_never_filled",
            setup_name=setup,
            strategy_id=a.strategy_id,
            closed_at=now,
        )

        # 7. Emit audit event + resolve the strike.
        emit_event(
            session,
            type="auto_close_entry_never_filled",
            strategy_id=a.strategy_id,
            symbol=a.symbol,
            asset_class=asset_class,
            payload={
                "setup": setup,
                "entry_coid": entry_coid,
                "entry_order_id": entry_order_id,
                "entry_px": entry_px,
                "qty": float(row.qty),
                "strike_count": existing.strike_count,
            },
        )
        existing.resolved = True
        existing.resolved_at = now
        existing.resolved_reason = "auto_close_entry_never_filled"
        resolved_count += 1
        log.warning(
            "AUTO_CLOSE_ENTRY_NEVER_FILLED symbol=%s setup=%s "
            "entry_coid=%s order_id=%s strike_count=%d",
            a.symbol, setup, entry_coid, entry_order_id,
            existing.strike_count,
        )

    return resolved_count


def detect_manual_close(
    *,
    alpaca: Any,
    store: MySQLStore,
    session: Session,
    anomalies: list,
    recent_fills: list[dict],
    cfg: ReconcilerConfig,
    now: datetime,
    asset_class: str | None = None,
) -> int:
    """Detect manual closes among mysql_only anomalies.

    A candidate is an mysql_only anomaly whose entry COID has filled at the
    broker (closed_orders shows it) AND whose recent_fills window contains
    no exit/stop/target COID for any open MySQL row on that symbol. This
    means the position once existed at the broker, the broker now reports
    zero, and we have no record of an aitrader-issued exit fill — i.e.
    something or someone else closed it.

    Confirmation gate: requires cfg.manual_close_confirm_cycles consecutive
    cycles of candidacy under strike key ``manual_close:{strategy_id}:{symbol}``.
    On confirmation, closes the MySQL row(s) with close_reason='manual_close'
    at the row's entry price (zero PnL — we have no broker-side close fill
    to price it from), inserts a manual_close_cooldowns row, emits a
    ``manual_close`` audit event, and resolves the strike with
    resolved_reason='manual_close_confirmed'.

    Per spec Decision 5, default cooldown is 60 minutes; if
    ``manual_close_cooldown_min`` is 0 the row is inserted with
    cooldown_until == started_at (audit-only mode — events emit but the
    filter never blocks).

    Returns the number of manual closes confirmed this cycle.
    """
    if cfg.shadow_mode:
        # Shadow mode runs detection but suppresses side effects. Emit a
        # single audit event per cycle that lists the candidates so the
        # operator can see what would have happened.
        for a in anomalies:
            if a.direction != "mysql_only":
                continue
            if not _is_manual_close_candidate(
                alpaca=alpaca, store=store, session=session, a=a,
                recent_fills=recent_fills,
            ):
                continue
            emit_event(
                session,
                type="manual_close_shadow",
                strategy_id=a.strategy_id,
                symbol=a.symbol,
                asset_class=asset_class,
                payload={
                    "mysql_qty": a.snapshot.get("mysql_qty"),
                    "broker_qty": a.snapshot.get("broker_qty"),
                },
            )
        return 0

    confirmed = 0
    confirm_threshold = max(1, cfg.manual_close_confirm_cycles)
    cooldown_minutes = max(0, cfg.manual_close_cooldown_min)

    for a in anomalies:
        if a.direction != "mysql_only":
            continue
        if not _is_manual_close_candidate(
            alpaca=alpaca, store=store, session=session, a=a,
            recent_fills=recent_fills,
        ):
            continue

        strike_key = f"manual_close:{a.strategy_id}:{a.symbol}"
        strike = session.query(StrikeRow).filter(
            StrikeRow.key == strike_key,
            StrikeRow.resolved == False,  # noqa: E712
        ).one_or_none()

        if strike is None:
            strike = StrikeRow(
                key=strike_key,
                direction="manual_close",
                strategy_id=a.strategy_id,
                symbol=a.symbol,
                asset_class=asset_class,
                strike_count=1,
                first_seen_at=now,
                last_seen_at=now,
                last_observed_state=a.snapshot,
                resolved=False,
            )
            session.add(strike)
        else:
            # Min-gap rate-limit identical to reconciler/strikes.process_anomaly.
            last_seen = strike.last_seen_at
            if last_seen.tzinfo is None:
                last_seen = last_seen.replace(tzinfo=timezone.utc)
            if (now - last_seen).total_seconds() < cfg.strike_min_gap_s:
                continue
            strike.strike_count += 1
            strike.last_seen_at = now
            strike.last_observed_state = a.snapshot
            if strike.asset_class is None and asset_class is not None:
                strike.asset_class = asset_class

        if strike.strike_count < confirm_threshold:
            emit_event(
                session,
                type="manual_close_candidate",
                strategy_id=a.strategy_id,
                symbol=a.symbol,
                asset_class=asset_class,
                payload={
                    "mysql_qty": a.snapshot.get("mysql_qty"),
                    "broker_qty": a.snapshot.get("broker_qty"),
                    "strike_count": strike.strike_count,
                    "recent_fills_count": len(recent_fills),
                },
            )
            continue

        # Confirmation reached. Idempotency: if an active cooldown already
        # exists for (strategy, symbol), emit redetected and resolve the
        # strike without re-closing or re-inserting.
        flat_symbol = a.symbol.replace("/", "")
        existing_cooldown = session.query(ManualCloseCooldownRow).filter(
            ManualCloseCooldownRow.strategy_id == a.strategy_id,
            ManualCloseCooldownRow.symbol == flat_symbol,
            ManualCloseCooldownRow.cleared_at.is_(None),
            ManualCloseCooldownRow.cooldown_until > now,
        ).one_or_none()
        if existing_cooldown is not None:
            emit_event(
                session,
                type="manual_close_redetected",
                strategy_id=a.strategy_id,
                symbol=a.symbol,
                asset_class=asset_class,
                payload={
                    "existing_cooldown_id": existing_cooldown.id,
                    "current_until": existing_cooldown.cooldown_until.isoformat(),
                },
            )
            strike.resolved = True
            strike.resolved_at = now
            strike.resolved_reason = "manual_close_redetected"
            continue

        # Close the MySQL row. Multiple rows may exist (cross-strategy or
        # duplicate-form historical artifact); position_closed handles the
        # multi-row case by archiving each. Use entry_px for zero PnL —
        # we have no broker-side fill to price the close from.
        rows = _open_rows_for_symbol(session, store, a.symbol)
        rows_for_strategy = [r for r in rows if r.strategy_id == a.strategy_id]
        if not rows_for_strategy:
            # Race: row already closed between candidacy check and now.
            # auto_clear_resolved will pick the strike up next cycle.
            continue

        primary = rows_for_strategy[0]
        setup = primary.setup_name
        closed_position_id = primary.id
        try:
            store.position_closed(
                symbol=a.symbol,
                exit_px=float(primary.entry_px),
                close_reason="manual_close",
                setup_name=setup,
                strategy_id=a.strategy_id,
                closed_at=now,
            )
        except Exception as exc:
            log.error(
                "MANUAL_CLOSE_DB_FAILED symbol=%s strategy_id=%s err=%s",
                a.symbol, a.strategy_id, exc, exc_info=True,
            )
            continue

        from datetime import timedelta
        cooldown_until = now + timedelta(minutes=cooldown_minutes)

        ev_id = emit_event(
            session,
            type="manual_close",
            strategy_id=a.strategy_id,
            symbol=a.symbol,
            asset_class=asset_class,
            payload={
                "mysql_qty": a.snapshot.get("mysql_qty"),
                "broker_qty": a.snapshot.get("broker_qty"),
                "cooldown_until": cooldown_until.isoformat(),
                "closed_position_id": closed_position_id,
                "setup": setup,
                "strike_count": strike.strike_count,
            },
        )

        # emit_event may or may not return the row id; flush so we can read
        # back the id of the just-added row when it doesn't.
        if not isinstance(ev_id, int):
            session.flush()
            last_event = session.query(EventRow).filter(
                EventRow.type == "manual_close",
                EventRow.strategy_id == a.strategy_id,
                EventRow.symbol == a.symbol,
            ).order_by(EventRow.id.desc()).first()
            ev_id = last_event.id if last_event is not None else None

        cooldown_row = ManualCloseCooldownRow(
            strategy_id=a.strategy_id,
            symbol=flat_symbol,
            asset_class=asset_class or primary.asset_class,
            started_at=now,
            cooldown_until=cooldown_until,
            last_broker_qty=Decimal("0"),
            last_mysql_qty=Decimal(str(a.snapshot.get("mysql_qty", 0.0))),
            closed_position_id=closed_position_id,
            reconciler_event_id=ev_id if isinstance(ev_id, int) else None,
        )
        session.add(cooldown_row)

        strike.resolved = True
        strike.resolved_at = now
        strike.resolved_reason = "manual_close_confirmed"
        confirmed += 1
        log.warning(
            "MANUAL_CLOSE_CONFIRMED strategy_id=%s symbol=%s "
            "cooldown_until=%s strike_count=%d",
            a.strategy_id, a.symbol, cooldown_until.isoformat(),
            strike.strike_count,
        )

    return confirmed


def _is_manual_close_candidate(
    *,
    alpaca: Any,
    store: MySQLStore,
    session: Session,
    a,
    recent_fills: list[dict],
) -> bool:
    """A candidate is an mysql_only anomaly where:

    1. There is exactly one open MySQL row owned by a.strategy_id on a.symbol
       (multi-row cases route to entry_never_filled or qty_drift instead).
    2. The row's entry_coid is non-empty and that COID is filled at the broker
       (looked up via list_orders status='closed').
    3. No fill in recent_fills carries an exit/stop/target COID matching this
       row's (strategy_name, setup_name, symbol).

    Returns True if the candidate qualifies for confirmation counting.
    """
    if a.strategy_id is None:
        return False
    candidates = store._get_symbol_candidates(a.symbol)
    rows = session.query(PositionRow).filter(
        PositionRow.strategy_id == a.strategy_id,
        PositionRow.symbol.in_(candidates),
        PositionRow.status == "open",
    ).all()
    if len(rows) != 1:
        return False
    row = rows[0]
    entry_coid = row.client_order_id
    if not entry_coid:
        # Pre-COID legacy row — operator must reconcile manually.
        return False

    # Entry COID must show as a filled order at the broker. If it's still
    # in any 'open' status, this is the entry_never_filled case (handled
    # by auto_resolve_mysql_only_entry_never_filled, not us).
    try:
        closed_orders = alpaca.list_orders(
            status="closed", symbols=[a.symbol], nested=False,
        ) or []
    except Exception as exc:
        log.warning(
            "MANUAL_CLOSE_LIST_ORDERS_FAILED symbol=%s err=%s",
            a.symbol, exc,
        )
        return False
    entry_filled = any(
        o.get("client_order_id") == entry_coid
        and (o.get("status") == "filled"
             or float(o.get("filled_qty") or 0) > 0)
        for o in closed_orders
    )
    if not entry_filled:
        return False

    # No exit COID in recent_fills means we have no record of an
    # aitrader-issued close. Match by (strategy, setup, symbol) regardless
    # of role — any exit/stop/target whose COID points at this row's
    # (strategy_name, setup) suppresses detection.
    parsed_strategy = None
    if a.strategy_id is not None:
        strow = session.query(StrategyRow).filter(
            StrategyRow.id == a.strategy_id,
        ).one_or_none()
        if strow is not None:
            parsed_strategy = strow.name
    if parsed_strategy is None:
        return False

    flat = a.symbol.replace("/", "")
    setup = row.setup_name
    for fill in recent_fills or []:
        fill_sym = (fill.get("symbol") or "").replace("/", "")
        if fill_sym != flat:
            continue
        parsed = parse_client_order_id(fill.get("client_order_id"))
        if parsed is None or parsed["role"] not in _EXIT_ROLES:
            continue
        if parsed["strategy"] == parsed_strategy and parsed["setup"] == setup:
            return False

    return True


def _split_qty(total_qty: float, max_qty_per_chunk: float) -> list[float]:
    """Split ``total_qty`` into chunks no larger than ``max_qty_per_chunk``.

    The last chunk holds the remainder, which is always > 0 (we never
    return a zero-qty chunk). All chunks except the last are exactly
    ``max_qty_per_chunk``; the last is total_qty - max * (n-1).
    """
    if max_qty_per_chunk <= 0:
        # Caller has already validated price > 0; this is defense-in-depth.
        return [total_qty]
    if total_qty <= max_qty_per_chunk:
        return [total_qty]
    n_full = int(total_qty // max_qty_per_chunk)
    remainder = total_qty - n_full * max_qty_per_chunk
    chunks = [max_qty_per_chunk] * n_full
    if remainder > 0:
        chunks.append(remainder)
    return chunks


def run_one_cycle(
    *,
    store: MySQLStore,
    alpaca: Any,
    cfg: ReconcilerConfig,
    last_orders_check_ts: datetime | None,
    now: datetime,
) -> datetime | None:
    """Run a single reconciliation cycle.

    Returns the new high-water mark for `last_orders_check_ts` if the cycle
    completed (advance to `now`), or None if any step skipped due to an
    Alpaca/IO error and the timestamp must NOT be advanced.
    """
    # 1. Pull broker truth.
    try:
        broker_positions = alpaca.get_positions()
        recent_fills = alpaca.list_orders(
            status="closed",
            after=last_orders_check_ts,
            nested=True,
        )
    except Exception as exc:
        log.error("RECONCILER_PULL_FAILED: %s", exc, exc_info=True)
        return None

    broker_norm = _broker_qty_by_symbol(broker_positions)
    asset_class = getattr(cfg, "asset_class", None)

    with Session(store._engine) as session:
        # 1.5. Heartbeat staleness check (before fills, so the alert is the
        # first artifact written this cycle). Scoped to this side's heartbeat
        # row so equity's stale alert doesn't fire because crypto stopped.
        hb_q = session.query(EventRow.created_at).filter(
            EventRow.type == "heartbeat",
        )
        if asset_class is not None:
            hb_q = hb_q.filter(EventRow.asset_class == asset_class)
        last_hb = hb_q.order_by(EventRow.created_at.desc()).first()
        if last_hb is not None:
            last_hb_ts = last_hb[0]
            if last_hb_ts.tzinfo is None:
                last_hb_ts = last_hb_ts.replace(tzinfo=timezone.utc)
            age_s = (now - last_hb_ts).total_seconds()
            if age_s > cfg.heartbeat_stale_after_s:
                emit_event(
                    session,
                    type="reconciler_heartbeat_stale",
                    asset_class=asset_class,
                    payload={
                        "last_seen_at": last_hb_ts.isoformat(),
                        "age_seconds": age_s,
                        "threshold_s": cfg.heartbeat_stale_after_s,
                    },
                )
                send_reconcile_heartbeat_stale(
                    last_seen_at=last_hb_ts,
                    age_seconds=age_s,
                    stale_threshold_s=cfg.heartbeat_stale_after_s,
                )

        # 2. Apply tagged fills (or shadow-log them).
        for fill in recent_fills:
            if cfg.shadow_mode:
                emit_event(
                    session,
                    type="shadow_would_apply_fill",
                    symbol=fill.get("symbol"),
                    asset_class=asset_class,
                    payload={
                        "alpaca_id": fill.get("id"),
                        "client_order_id": fill.get("client_order_id"),
                    },
                )
            else:
                apply_tagged_fill(
                    session, fill, store, cycle_asset_class=asset_class,
                )
        session.commit()

        # 3. Check the invariant against post-fill state.
        anomalies = check_invariant(
            session, store, broker_norm,
            qty_eps=cfg.qty_eps, asset_class=asset_class,
        )

        # 4. Process anomalies through the strike rule.
        if not cfg.shadow_mode:
            for a in anomalies:
                outcome = process_anomaly(session, a, cfg, now=now)
                if outcome.alert_sent:
                    strategy_name = None
                    if a.strategy_id is not None:
                        strow = session.query(StrategyRow).filter(
                            StrategyRow.id == a.strategy_id,
                        ).one_or_none()
                        if strow:
                            strategy_name = strow.name
                    send_reconcile_alert(
                        direction=a.direction,
                        symbol=a.symbol,
                        strategy_name=strategy_name,
                        snapshot=a.snapshot,
                        strike_count=outcome.strike_count,
                        strike_threshold=cfg.strike_threshold,
                    )

        # 4.5. Auto-close broker_only anomalies whose strike has confirmed
        # them as unmanaged. Strike-gated so transient races (in-flight fill
        # not yet written to MySQL) self-heal before we flatten anything.
        broker_positions_by_sym = _broker_position_by_symbol(broker_positions)
        auto_close_broker_only(
            alpaca=alpaca, store=store, session=session,
            broker_positions=broker_positions_by_sym,
            anomalies=anomalies, cfg=cfg, now=now,
            asset_class=asset_class,
        )

        # 4.6. Auto-resolve qty_drift anomalies. recent_fills carries the
        # COIDs we use to attribute drift to a specific strategy/setup when
        # multiple rows hold the symbol.
        auto_resolve_qty_drift(
            alpaca=alpaca, store=store, session=session,
            broker_positions=broker_positions_by_sym,
            anomalies=anomalies, recent_fills=recent_fills,
            cfg=cfg, now=now,
            asset_class=asset_class,
        )

        # 4.7. Auto-resolve mysql_only anomalies whose entry order is still
        # sitting unfilled at the broker. Self-heals the optimistic-insert
        # case (limit-bracket parent never hit during the session).
        auto_resolve_mysql_only_entry_never_filled(
            alpaca=alpaca, store=store, session=session,
            anomalies=anomalies, recent_fills=recent_fills,
            cfg=cfg, now=now, asset_class=asset_class,
        )

        # 4.8. Detect manual closes (operator or external risk system closed
        # a position at the broker). When an mysql_only anomaly's entry COID
        # is filled at the broker but no exit COID for it appears in this
        # cycle's recent_fills, the position was closed externally — close
        # the MySQL row and start a cooldown so the strategy doesn't re-enter
        # on the next signal. See specs/manual-close-cooldown.md.
        detect_manual_close(
            alpaca=alpaca, store=store, session=session,
            anomalies=anomalies, recent_fills=recent_fills,
            cfg=cfg, now=now, asset_class=asset_class,
        )

        # 5. Auto-clear strikes whose anomaly is no longer present.
        auto_clear_resolved(
            session,
            current_anomaly_keys={a.key for a in anomalies},
            now=now,
            asset_class=asset_class,
        )

        # 6. Heartbeat.
        emit_event(
            session,
            type="heartbeat",
            asset_class=asset_class,
            payload={
                "broker_symbols": len(broker_norm),
                "anomalies": len(anomalies),
                "shadow_mode": cfg.shadow_mode,
                "asset_class": asset_class,
            },
        )

        session.commit()

    log.info(
        "RECONCILER_CYCLE_DONE asset_class=%s broker_symbols=%d "
        "anomalies=%d fills=%d shadow=%s",
        asset_class, len(broker_norm), len(anomalies),
        len(recent_fills), cfg.shadow_mode,
    )
    return now


def main() -> int:
    """Entry point: wire env config, load state, run forever."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    cfg = ReconcilerConfig.from_env()
    log.info(
        "RECONCILER_STARTING asset_class=%s interval_s=%d threshold=%d shadow=%s",
        cfg.asset_class, cfg.interval_s, cfg.strike_threshold, cfg.shadow_mode,
    )

    # Per-asset-class strategy row so each reconciler container has its own
    # audit trail and the two never collide on upsert.
    store = MySQLStore(strategy_name=f"reconciler-{cfg.asset_class}")
    store.ensure_schema()
    store.upsert_strategy()

    state = load_state(cfg.state_file_path)
    log.info(
        "RECONCILER_LOADED_STATE asset_class=%s last_orders_check_ts=%s",
        cfg.asset_class, state.last_orders_check_ts,
    )

    # One Alpaca account per side (PR #83). Each reconciler talks to its
    # own broker; an outage on the other side can't short-circuit this
    # cycle's get_positions / list_orders.
    alpaca = AlpacaClient(asset_class=cfg.asset_class)

    while True:
        now = datetime.now(timezone.utc)
        try:
            advanced_to = run_one_cycle(
                store=store, alpaca=alpaca, cfg=cfg,
                last_orders_check_ts=state.last_orders_check_ts, now=now,
            )
            if advanced_to is not None:
                state.last_orders_check_ts = advanced_to
                save_state(
                    cfg.state_file_path,
                    last_orders_check_ts=state.last_orders_check_ts,
                )
        except Exception as exc:
            log.error("RECONCILER_CYCLE_CRASHED: %s", exc, exc_info=True)
        time.sleep(cfg.interval_s)


if __name__ == "__main__":
    raise SystemExit(main())
