"""Read-only repo for the reconciliation dashboard tab.

Mirrors the style of ui/data/positions_repo.py: SQL via the shared engine,
pandas DataFrames out.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
from sqlalchemy import text

from ui.data.db import get_engine


def get_unresolved_strikes(asset_class: str | None = None) -> pd.DataFrame:
    """All unresolved reconciliation_strikes joined to strategy name.

    ``asset_class`` filters the result to that side. None returns all rows
    (used by audit tooling and tests).

    Columns: id, key, direction, symbol, strategy (str|None), asset_class,
             strike_count, first_seen_at, last_seen_at, last_observed_state.
    Ordered: most recently seen first.
    """
    eng = get_engine()
    where = "WHERE r.resolved = 0"
    params: dict = {}
    if asset_class is not None:
        where += " AND r.asset_class = :asset_class"
        params["asset_class"] = asset_class
    with eng.connect() as conn:
        df = pd.read_sql(
            text(f"""
                SELECT r.id, r.`key`, r.direction, r.symbol,
                       s.name AS strategy, r.asset_class,
                       r.strike_count, r.first_seen_at, r.last_seen_at,
                       r.last_observed_state
                FROM reconciliation_strikes r
                LEFT JOIN strategies s ON s.id = r.strategy_id
                {where}
                ORDER BY r.last_seen_at DESC
            """),
            conn,
            params=params,
        )
    return df


def get_recent_events(
    limit: int = 50,
    asset_class: str | None = None,
) -> pd.DataFrame:
    """Most recent reconciliation_events.

    ``asset_class`` scopes to one side; None returns all rows.

    Columns: id, type, strategy (str|None), symbol, asset_class, payload,
             created_at.
    Ordered: newest first.
    """
    eng = get_engine()
    where = ""
    params: dict = {"limit": int(limit)}
    if asset_class is not None:
        where = "WHERE e.asset_class = :asset_class"
        params["asset_class"] = asset_class
    with eng.connect() as conn:
        df = pd.read_sql(
            text(f"""
                SELECT e.id, e.type, s.name AS strategy, e.symbol,
                       e.asset_class, e.payload, e.created_at
                FROM reconciliation_events e
                LEFT JOIN strategies s ON s.id = e.strategy_id
                {where}
                ORDER BY e.created_at DESC
                LIMIT :limit
            """),
            conn,
            params=params,
        )
    return df


def get_heartbeat_freshness(asset_class: str | None = None) -> dict:
    """Last `heartbeat` event timestamp and its age in seconds.

    ``asset_class`` scopes to that reconciler's heartbeat row. None matches
    any heartbeat (legacy callers).

    Returns:
        {"last_seen_at": datetime | None, "age_seconds": float | None}
    """
    eng = get_engine()
    where = "WHERE type = 'heartbeat'"
    params: dict = {}
    if asset_class is not None:
        where += " AND asset_class = :asset_class"
        params["asset_class"] = asset_class
    with eng.connect() as conn:
        result = conn.execute(text(f"""
            SELECT MAX(created_at) FROM reconciliation_events
            {where}
        """), params).first()
    last_seen = result[0] if result else None
    if last_seen is None:
        return {"last_seen_at": None, "age_seconds": None}
    # SQLite returns datetime values as strings; parse them if needed.
    if isinstance(last_seen, str):
        last_seen = datetime.fromisoformat(last_seen)
    if last_seen.tzinfo is None:
        last_seen = last_seen.replace(tzinfo=timezone.utc)
    age = (datetime.now(timezone.utc) - last_seen).total_seconds()
    return {"last_seen_at": last_seen, "age_seconds": age}
