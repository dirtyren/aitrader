"""Apply a single Alpaca filled order to the MySQL state.

Pure function (modulo the SQLAlchemy session it receives). The caller owns
session lifecycle (session.commit).

Decision tree:
    - COID missing or unparseable             → untagged_fill event, no mutation.
    - COID names an unknown strategy          → untagged_fill event, no mutation.
    - role == 'entry' / 'adopted', no row     → INSERT (crash-before-write recovery).
    - role == 'entry' / 'adopted', row exists → idempotent noop (no event).
    - role in ('exit', 'stop', 'target'),
        matching open row in MySQL            → close row + write trade row.
    - role in ('exit', 'stop', 'target'),
        no matching open row                  → idempotent noop (no event).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from broker.client_order_id import parse_client_order_id
from reconciler.events import emit_event
from state.mysql_store import MySQLStore, StrategyRow

log = logging.getLogger(__name__)

_ENTRY_ROLES = frozenset({"entry", "adopted"})
_EXIT_ROLES = frozenset({"exit", "stop", "target"})


def _resolve_strategy_id(session: Session, strategy_name: str) -> int | None:
    row = session.query(StrategyRow).filter(
        StrategyRow.name == strategy_name,
    ).one_or_none()
    return row.id if row else None


def _parse_fill_time(raw: str | None) -> datetime:
    if not raw:
        return datetime.now(timezone.utc)
    try:
        # Alpaca returns "2026-05-28T14:00:00.123Z"
        s = raw.replace("Z", "+00:00")
        return datetime.fromisoformat(s)
    except ValueError:
        return datetime.now(timezone.utc)


def apply_tagged_fill(
    session: Session,
    fill: dict[str, Any],
    store: MySQLStore,
    *,
    cycle_asset_class: str | None = None,
) -> None:
    """Apply one filled order. The caller is responsible for session.commit().

    ``cycle_asset_class`` is the reconciler instance's asset class, stamped
    on every emit_event so the dashboard subtabs can scope. The fill itself
    still carries its own ``asset_class`` (used for INSERT routing on entry
    recovery), which is normally identical because each per-process
    reconciler only sees its own broker's fills.
    """
    coid = fill.get("client_order_id")
    parsed = parse_client_order_id(coid)
    if parsed is None:
        emit_event(
            session,
            type="untagged_fill",
            symbol=fill.get("symbol"),
            asset_class=cycle_asset_class,
            payload={"alpaca_id": fill.get("id"), "client_order_id": coid},
        )
        return

    strategy_id = _resolve_strategy_id(session, parsed["strategy"])
    if strategy_id is None:
        emit_event(
            session,
            type="untagged_fill",
            symbol=fill.get("symbol"),
            asset_class=cycle_asset_class,
            payload={
                "alpaca_id": fill.get("id"),
                "client_order_id": coid,
                "reason": "unknown_strategy",
                "strategy": parsed["strategy"],
            },
        )
        return

    role = parsed["role"]
    symbol = parsed["symbol"]
    setup = parsed["setup"]

    if role in _ENTRY_ROLES:
        existing = store.find_open_position_by_coid(coid)
        if existing is not None:
            return  # idempotent noop, already applied
        # Crash-before-write recovery: insert the row.
        try:
            qty = float(fill.get("filled_qty") or 0)
            entry_px = float(fill.get("filled_avg_price") or 0)
        except (TypeError, ValueError):
            qty = 0.0
            entry_px = 0.0
        if qty <= 0 or entry_px <= 0:
            emit_event(
                session,
                type="untagged_fill",
                strategy_id=strategy_id,
                symbol=symbol,
                asset_class=cycle_asset_class,
                payload={
                    "alpaca_id": fill.get("id"),
                    "client_order_id": coid,
                    "reason": "missing_fill_data",
                    "filled_qty": fill.get("filled_qty"),
                    "filled_avg_price": fill.get("filled_avg_price"),
                },
            )
            return
        side = "long" if fill.get("side") == "buy" else "short"
        opened_at = _parse_fill_time(fill.get("filled_at"))
        asset_class = "crypto" if fill.get("asset_class") == "crypto" else "equity"
        store.insert_position_from_fill(
            strategy_id=strategy_id,
            setup_name=setup,
            symbol=symbol,
            side=side,
            qty=qty,
            entry_px=entry_px,
            opened_at=opened_at,
            asset_class=asset_class,
            client_order_id=coid,
        )
        emit_event(
            session,
            type="tagged_entry_inserted",
            strategy_id=strategy_id,
            symbol=symbol,
            asset_class=cycle_asset_class,
            payload={"client_order_id": coid, "alpaca_id": fill.get("id")},
        )
        return

    if role in _EXIT_ROLES:
        open_row = store.find_open_position_by_setup(strategy_id, symbol, setup)
        if open_row is None:
            return  # idempotent noop
        exit_px = float(fill.get("filled_avg_price") or 0)
        store.position_closed(
            symbol=symbol,
            exit_px=exit_px,
            close_reason="broker_fill",
            setup_name=setup,
            exit_client_order_id=coid,
            strategy_id=strategy_id,
        )
        emit_event(
            session,
            type="tagged_fill_applied",
            strategy_id=strategy_id,
            symbol=symbol,
            asset_class=cycle_asset_class,
            payload={
                "client_order_id": coid,
                "alpaca_id": fill.get("id"),
                "role": role,
            },
        )
        return

    # Defensive — unreachable given the role enum.
    log.warning("RECONCILER_UNKNOWN_ROLE coid=%s role=%s", coid, role)
