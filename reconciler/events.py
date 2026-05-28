"""Reconciliation event log writer.

A `reconciliation_events` row is the audit-log artifact of every interesting
moment: heartbeat, untagged fill seen, anomaly confirmed at strike N,
operator action via the CLI (Plan 4).
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from state.mysql_store import EventRow

log = logging.getLogger(__name__)


def emit_event(
    session: Session,
    *,
    type: str,
    strategy_id: int | None = None,
    symbol: str | None = None,
    payload: dict[str, Any] | None = None,
) -> None:
    """Insert a reconciliation_events row.

    The caller owns the session and is responsible for commit().
    """
    row = EventRow(
        type=type,
        strategy_id=strategy_id,
        symbol=symbol,
        payload=payload,
    )
    session.add(row)
