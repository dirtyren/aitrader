"""Multi-strike confirmation rule.

An anomaly is observed → strike row inserted-or-updated. After N consecutive
observations spaced ≥ min_gap_s apart, the anomaly is "frozen" — alert sent,
strike marked resolved as 'frozen_for_operator'. Anomalies that disappear
before reaching N self-heal via auto_clear_resolved.

No mutation of positions/trades happens here — strikes only emit alerts and
write events. The actual freeze behavior is operator-driven (Plan 4).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable

from sqlalchemy import or_
from sqlalchemy.orm import Session

from reconciler.config import ReconcilerConfig
from reconciler.events import emit_event
from reconciler.invariant import Anomaly
from state.mysql_store import StrikeRow

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class StrikeOutcome:
    action: str       # 'noop' | 'logged_strike1' | 'alerted' | 'frozen' | 'self_healed'
    strike_count: int
    alert_sent: bool


def _find_unresolved(session: Session, key: str) -> StrikeRow | None:
    return session.query(StrikeRow).filter(
        StrikeRow.key == key,
        StrikeRow.resolved == False,  # noqa: E712 — SQLAlchemy idiom
    ).one_or_none()


def process_anomaly(
    session: Session,
    anomaly: Anomaly,
    cfg: ReconcilerConfig,
    *,
    now: datetime,
) -> StrikeOutcome:
    """Look up or insert a strike row for `anomaly.key`, advance the count.

    Returns the outcome describing what happened this cycle.
    """
    existing = _find_unresolved(session, anomaly.key)

    if existing is None:
        row = StrikeRow(
            key=anomaly.key,
            direction=anomaly.direction,
            strategy_id=anomaly.strategy_id,
            symbol=anomaly.symbol,
            asset_class=anomaly.asset_class,
            strike_count=1,
            first_seen_at=now,
            last_seen_at=now,
            last_observed_state=anomaly.snapshot,
            resolved=False,
        )
        session.add(row)
        return StrikeOutcome(action="logged_strike1", strike_count=1, alert_sent=False)

    # Adopt legacy NULLs by stamping the running side's class on update.
    if existing.asset_class is None and anomaly.asset_class is not None:
        existing.asset_class = anomaly.asset_class

    # Already at threshold and frozen — treat further observations as noop.
    if existing.strike_count >= cfg.strike_threshold:
        existing.last_seen_at = now
        existing.last_observed_state = anomaly.snapshot
        return StrikeOutcome(
            action="frozen", strike_count=existing.strike_count, alert_sent=False,
        )

    # Min-gap rate limit: same anomaly observed too soon → noop.
    # SQLite returns naive datetimes for timezone-aware columns; normalise so
    # the subtraction always works regardless of backend.
    last_seen = existing.last_seen_at
    if last_seen.tzinfo is None:
        from datetime import timezone as _tz
        last_seen = last_seen.replace(tzinfo=_tz.utc)
    elapsed = (now - last_seen).total_seconds()
    if elapsed < cfg.strike_min_gap_s:
        return StrikeOutcome(
            action="noop", strike_count=existing.strike_count, alert_sent=False,
        )

    existing.strike_count += 1
    existing.last_seen_at = now
    existing.last_observed_state = anomaly.snapshot

    if existing.strike_count >= cfg.strike_threshold:
        # Reached threshold this cycle. Per spec: alert + freeze.
        emit_event(
            session,
            type=f"{anomaly.direction}_confirmed",
            strategy_id=anomaly.strategy_id,
            symbol=anomaly.symbol,
            asset_class=anomaly.asset_class,
            payload={
                "key": anomaly.key,
                "strike_count": existing.strike_count,
                "snapshot": anomaly.snapshot,
            },
        )
        return StrikeOutcome(
            action="frozen", strike_count=existing.strike_count, alert_sent=True,
        )

    return StrikeOutcome(
        action="alerted", strike_count=existing.strike_count, alert_sent=True,
    )


def auto_clear_resolved(
    session: Session,
    *,
    current_anomaly_keys: Iterable[str],
    now: datetime,
    asset_class: str | None = None,
) -> list[str]:
    """Mark unresolved strikes whose anomaly is no longer present as self_healed.

    When ``asset_class`` is set (per-process reconciler), only strikes on
    that side are considered. Pre-migration rows with NULL ``asset_class``
    are also adopted so they don't linger forever — the next cycle that
    sees their anomaly will re-stamp them with the running side's class.

    Returns the list of keys that were cleared.
    """
    current_set = set(current_anomaly_keys)
    cleared: list[str] = []
    q = session.query(StrikeRow).filter(StrikeRow.resolved == False)  # noqa: E712
    if asset_class is not None:
        # `asset_class IS NULL OR = :ac` — adopt legacy rows on whichever
        # side runs the next cycle; if one side hasn't started, the other
        # will not orphan them.
        q = q.filter(or_(
            StrikeRow.asset_class == asset_class,
            StrikeRow.asset_class.is_(None),
        ))
    for row in q.all():
        if row.key in current_set:
            continue
        row.resolved = True
        row.resolved_at = now
        row.resolved_reason = "self_healed"
        row.strike_count = 0
        cleared.append(row.key)
    return cleared
