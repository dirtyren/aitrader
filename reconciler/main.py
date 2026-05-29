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
from state.mysql_store import EventRow, MySQLStore

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
