"""Tests for the heartbeat-staleness detection inside run_one_cycle."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from reconciler.config import ReconcilerConfig
from reconciler.main import run_one_cycle
from state.mysql_store import (
    Base,
    EventRow,
    MySQLStore,
    StrategyRow,
)


@pytest.fixture
def store():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    s = MySQLStore.__new__(MySQLStore)
    s._engine = engine
    s.strategy_name = "reconciler"
    s._log = logging.getLogger("test_staleness")
    with Session(engine) as session:
        session.add(StrategyRow(name="vwap_wave"))
        session.commit()
        s._strategy_id = session.query(StrategyRow).one().id
    return s


def _cfg(stale_after_s=300):
    return ReconcilerConfig(
        interval_s=30, strike_threshold=3, strike_min_gap_s=60,
        qty_eps=1e-6, shadow_mode=False,
        state_file_path="/tmp/state.json",
        heartbeat_stale_after_s=stale_after_s,
        auto_close_max_notional_usd=190_000.0,
        auto_close_dust_usd=1.0,
    )


def test_no_alert_when_no_previous_heartbeat(store):
    """First-ever cycle has no prior heartbeat — no stale alert."""
    alpaca = MagicMock()
    alpaca.get_positions.return_value = []
    alpaca.list_orders.return_value = []
    cfg = _cfg()
    now = datetime(2026, 5, 28, 14, 0, tzinfo=timezone.utc)

    run_one_cycle(store=store, alpaca=alpaca, cfg=cfg,
                  last_orders_check_ts=None, now=now)

    with Session(store._engine) as session:
        types = [e.type for e in session.query(EventRow).all()]
        assert "heartbeat" in types
        assert "reconciler_heartbeat_stale" not in types


def test_no_alert_when_recent_heartbeat(store):
    """Last heartbeat was within the threshold → no alert."""
    base = datetime(2026, 5, 28, 14, 0, tzinfo=timezone.utc)
    with Session(store._engine) as session:
        session.add(EventRow(
            type="heartbeat",
            created_at=base - timedelta(seconds=60),
        ))
        session.commit()

    alpaca = MagicMock()
    alpaca.get_positions.return_value = []
    alpaca.list_orders.return_value = []
    cfg = _cfg(stale_after_s=300)

    run_one_cycle(store=store, alpaca=alpaca, cfg=cfg,
                  last_orders_check_ts=None, now=base)

    with Session(store._engine) as session:
        types = [e.type for e in session.query(EventRow).all()]
        assert "reconciler_heartbeat_stale" not in types


def test_alert_when_heartbeat_older_than_threshold(store):
    """Last heartbeat older than threshold → emit stale event."""
    base = datetime(2026, 5, 28, 14, 0, tzinfo=timezone.utc)
    with Session(store._engine) as session:
        session.add(EventRow(
            type="heartbeat",
            created_at=base - timedelta(seconds=600),
        ))
        session.commit()

    alpaca = MagicMock()
    alpaca.get_positions.return_value = []
    alpaca.list_orders.return_value = []
    cfg = _cfg(stale_after_s=300)

    run_one_cycle(store=store, alpaca=alpaca, cfg=cfg,
                  last_orders_check_ts=None, now=base)

    with Session(store._engine) as session:
        events = session.query(EventRow).order_by(EventRow.id).all()
        types = [e.type for e in events]
        assert "reconciler_heartbeat_stale" in types
        assert "heartbeat" in types
        # The stale event must be written BEFORE the new heartbeat
        stale_id = next(e.id for e in events if e.type == "reconciler_heartbeat_stale")
        new_hb_id = max(e.id for e in events if e.type == "heartbeat")
        assert stale_id < new_hb_id
