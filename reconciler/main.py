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
from typing import Any

from sqlalchemy.orm import Session

from broker.alpaca_client import AlpacaClient
from notifications import send_reconcile_alert, send_reconcile_heartbeat_stale
from reconciler.config import ReconcilerConfig
from reconciler.events import emit_event
from reconciler.fills import apply_tagged_fill
from reconciler.invariant import check_invariant
from reconciler.state import load_state, save_state
from reconciler.strikes import auto_clear_resolved, process_anomaly
from state.mysql_store import EventRow, MySQLStore, StrikeRow

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

        all_ok = True
        order_ids: list[str | None] = []
        for chunk_qty in chunks:
            try:
                order = alpaca.submit_order(
                    symbol=broker_symbol, qty=chunk_qty, side=side,
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

    with Session(store._engine) as session:
        # 1.5. Heartbeat staleness check (before fills, so the alert is the
        # first artifact written this cycle).
        last_hb = session.query(EventRow.created_at).filter(
            EventRow.type == "heartbeat",
        ).order_by(EventRow.created_at.desc()).first()
        if last_hb is not None:
            last_hb_ts = last_hb[0]
            if last_hb_ts.tzinfo is None:
                last_hb_ts = last_hb_ts.replace(tzinfo=timezone.utc)
            age_s = (now - last_hb_ts).total_seconds()
            if age_s > cfg.heartbeat_stale_after_s:
                emit_event(
                    session,
                    type="reconciler_heartbeat_stale",
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
                    payload={
                        "alpaca_id": fill.get("id"),
                        "client_order_id": fill.get("client_order_id"),
                    },
                )
            else:
                apply_tagged_fill(session, fill, store)
        session.commit()

        # 3. Check the invariant against post-fill state.
        anomalies = check_invariant(
            session, store, broker_norm, qty_eps=cfg.qty_eps,
        )

        # 4. Process anomalies through the strike rule.
        if not cfg.shadow_mode:
            for a in anomalies:
                outcome = process_anomaly(session, a, cfg, now=now)
                if outcome.alert_sent:
                    strategy_name = None
                    if a.strategy_id is not None:
                        from state.mysql_store import StrategyRow
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
        )

        # 5. Auto-clear strikes whose anomaly is no longer present.
        auto_clear_resolved(
            session,
            current_anomaly_keys={a.key for a in anomalies},
            now=now,
        )

        # 6. Heartbeat.
        emit_event(
            session,
            type="heartbeat",
            payload={
                "broker_symbols": len(broker_norm),
                "anomalies": len(anomalies),
                "shadow_mode": cfg.shadow_mode,
            },
        )

        session.commit()

    log.info(
        "RECONCILER_CYCLE_DONE broker_symbols=%d anomalies=%d fills=%d shadow=%s",
        len(broker_norm), len(anomalies), len(recent_fills), cfg.shadow_mode,
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
        "RECONCILER_STARTING interval_s=%d threshold=%d shadow=%s",
        cfg.interval_s, cfg.strike_threshold, cfg.shadow_mode,
    )

    store = MySQLStore(strategy_name="reconciler")
    store.ensure_schema()
    store.upsert_strategy()

    state = load_state(cfg.state_file_path)
    log.info("RECONCILER_LOADED_STATE last_orders_check_ts=%s",
             state.last_orders_check_ts)

    alpaca = AlpacaClient()

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
