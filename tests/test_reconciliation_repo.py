"""Tests for ui/data/reconciliation_repo.py — read-only dashboard repo."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from state.mysql_store import (
    Base,
    EventRow,
    StrategyRow,
    StrikeRow,
)
from ui.data import reconciliation_repo as repo


@pytest.fixture
def engine(monkeypatch):
    eng = create_engine("sqlite://")
    Base.metadata.create_all(eng)
    with Session(eng) as session:
        session.add_all([
            StrategyRow(name="vwap_wave"),
            StrategyRow(name="rsi_equity"),
        ])
        session.commit()

    # Force the repo to use this engine instead of the real MySQL one.
    from ui.data import db
    monkeypatch.setattr(db, "_engine", eng)
    monkeypatch.setattr(db, "get_engine", lambda: eng)
    return eng


def _add_strike(engine, *, key, direction, symbol, strategy_id=None,
                count=1, last_seen_offset_s=0, resolved=False):
    base = datetime(2026, 5, 28, 14, 0, tzinfo=timezone.utc)
    with Session(engine) as session:
        session.add(StrikeRow(
            key=key, direction=direction, symbol=symbol,
            strategy_id=strategy_id, strike_count=count,
            first_seen_at=base,
            last_seen_at=base + timedelta(seconds=last_seen_offset_s),
            last_observed_state={"mysql_sum": 2.0, "broker_qty": 1.0},
            resolved=resolved,
        ))
        session.commit()


def _add_event(engine, *, type_, symbol=None, strategy_id=None,
               offset_s=0, payload=None):
    base = datetime(2026, 5, 28, 14, 0, tzinfo=timezone.utc)
    with Session(engine) as session:
        session.add(EventRow(
            type=type_, symbol=symbol, strategy_id=strategy_id,
            payload=payload or {},
            created_at=base + timedelta(seconds=offset_s),
        ))
        session.commit()


# ── strikes ──────────────────────────────────────────────────────────


def test_get_unresolved_strikes_returns_dataframe(engine):
    _add_strike(engine, key="qty_drift:AAPL", direction="qty_drift",
                symbol="AAPL")
    _add_strike(engine, key="qty_drift:OLD", direction="qty_drift",
                symbol="OLD", resolved=True)
    df = repo.get_unresolved_strikes()
    assert isinstance(df, pd.DataFrame)
    assert len(df) == 1
    assert df.iloc[0]["symbol"] == "AAPL"
    assert "direction" in df.columns
    assert "strike_count" in df.columns


def test_get_unresolved_strikes_joins_strategy_name(engine):
    with Session(engine) as session:
        strategy_id = session.query(StrategyRow).filter(
            StrategyRow.name == "vwap_wave"
        ).one().id
    _add_strike(engine, key=f"mysql_only:{strategy_id}:AAPL",
                direction="mysql_only", symbol="AAPL",
                strategy_id=strategy_id)
    df = repo.get_unresolved_strikes()
    assert df.iloc[0]["strategy"] == "vwap_wave"


def test_get_unresolved_strikes_qty_drift_has_no_strategy(engine):
    _add_strike(engine, key="qty_drift:AAPL", direction="qty_drift",
                symbol="AAPL", strategy_id=None)
    df = repo.get_unresolved_strikes()
    # NULL strategy → empty / NaN, not crash
    assert df.iloc[0]["strategy"] in (None, "", float("nan")) or \
           pd.isna(df.iloc[0]["strategy"])


# ── events ───────────────────────────────────────────────────────────


def test_get_recent_events_orders_newest_first(engine):
    _add_event(engine, type_="heartbeat", offset_s=60)
    _add_event(engine, type_="heartbeat", offset_s=120)
    _add_event(engine, type_="heartbeat", offset_s=0)
    df = repo.get_recent_events(limit=10)
    assert list(df["type"]) == ["heartbeat", "heartbeat", "heartbeat"]
    timestamps = list(df["created_at"])
    assert timestamps == sorted(timestamps, reverse=True)


def test_get_recent_events_respects_limit(engine):
    for i in range(5):
        _add_event(engine, type_=f"e{i}", offset_s=i)
    df = repo.get_recent_events(limit=2)
    assert len(df) == 2


# ── heartbeat freshness ──────────────────────────────────────────────


def test_get_heartbeat_freshness_returns_age(engine):
    _add_event(engine, type_="heartbeat", offset_s=0)
    info = repo.get_heartbeat_freshness()
    assert info["last_seen_at"] is not None
    assert info["age_seconds"] is not None
    assert info["age_seconds"] >= 0


def test_get_heartbeat_freshness_no_heartbeat_yet(engine):
    info = repo.get_heartbeat_freshness()
    assert info["last_seen_at"] is None
    assert info["age_seconds"] is None


def test_get_heartbeat_freshness_ignores_other_event_types(engine):
    _add_event(engine, type_="untagged_fill", offset_s=0)
    info = repo.get_heartbeat_freshness()
    assert info["last_seen_at"] is None
