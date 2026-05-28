"""Tests for the multi-strike confirmation rule."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from reconciler.config import ReconcilerConfig
from reconciler.invariant import Anomaly
from reconciler.strikes import auto_clear_resolved, process_anomaly
from state.mysql_store import Base, EventRow, StrikeRow, StrategyRow


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        s.add(StrategyRow(name="vwap_wave"))
        s.commit()
        yield s


def _cfg(threshold=3, min_gap_s=60):
    return ReconcilerConfig(
        interval_s=30, strike_threshold=threshold, strike_min_gap_s=min_gap_s,
        qty_eps=1e-6, shadow_mode=False,
        state_file_path="/tmp/state.json",
    )


def _anomaly(direction="qty_drift", symbol="AAPL", strategy_id=None):
    return Anomaly(
        direction=direction, symbol=symbol, strategy_id=strategy_id,
        snapshot={"mysql_sum": 2.0, "broker_qty": 1.0},
    )


def test_first_observation_creates_row_at_strike1(session):
    cfg = _cfg()
    a = _anomaly()
    now = datetime(2026, 5, 28, 14, 0, tzinfo=timezone.utc)

    outcome = process_anomaly(session, a, cfg, now=now)
    session.commit()

    assert outcome.action == "logged_strike1"
    assert outcome.strike_count == 1
    assert outcome.alert_sent is False
    rows = session.query(StrikeRow).all()
    assert len(rows) == 1
    assert rows[0].strike_count == 1
    assert rows[0].resolved is False


def test_repeated_observations_increment_strike_count(session):
    cfg = _cfg()
    a = _anomaly()
    base = datetime(2026, 5, 28, 14, 0, tzinfo=timezone.utc)

    o1 = process_anomaly(session, a, cfg, now=base)
    session.commit()
    o2 = process_anomaly(session, a, cfg, now=base + timedelta(seconds=cfg.strike_min_gap_s + 1))
    session.commit()

    assert o1.strike_count == 1
    assert o2.strike_count == 2
    assert o2.action == "alerted"
    assert o2.alert_sent is True


def test_strike_threshold_triggers_frozen(session):
    cfg = _cfg(threshold=3)
    a = _anomaly(direction="mysql_only", strategy_id=1)
    base = datetime(2026, 5, 28, 14, 0, tzinfo=timezone.utc)

    for i in range(3):
        process_anomaly(session, a, cfg, now=base + timedelta(seconds=cfg.strike_min_gap_s * i + i))
        session.commit()
    rows = session.query(StrikeRow).all()
    assert len(rows) == 1
    assert rows[0].strike_count == 3
    last = process_anomaly(session, a, cfg, now=base + timedelta(seconds=cfg.strike_min_gap_s * 4))
    session.commit()
    assert last.action == "frozen" or last.strike_count >= 3


def test_observations_within_min_gap_are_noop(session):
    cfg = _cfg(min_gap_s=60)
    a = _anomaly()
    base = datetime(2026, 5, 28, 14, 0, tzinfo=timezone.utc)

    process_anomaly(session, a, cfg, now=base)
    session.commit()
    o2 = process_anomaly(session, a, cfg, now=base + timedelta(seconds=10))
    session.commit()

    assert o2.action == "noop"
    assert o2.strike_count == 1
    rows = session.query(StrikeRow).all()
    assert rows[0].strike_count == 1


def test_auto_clear_resolved_marks_disappeared_anomalies(session):
    cfg = _cfg()
    a = _anomaly()
    base = datetime(2026, 5, 28, 14, 0, tzinfo=timezone.utc)

    process_anomaly(session, a, cfg, now=base)
    session.commit()
    cleared_keys = auto_clear_resolved(session, current_anomaly_keys=set(), now=base + timedelta(seconds=cfg.strike_min_gap_s + 1))
    session.commit()

    assert cleared_keys == [a.key]
    rows = session.query(StrikeRow).all()
    assert rows[0].resolved is True
    assert rows[0].resolved_reason == "self_healed"
    assert rows[0].strike_count == 0


def test_auto_clear_skips_anomalies_still_present(session):
    cfg = _cfg()
    a = _anomaly()
    base = datetime(2026, 5, 28, 14, 0, tzinfo=timezone.utc)

    process_anomaly(session, a, cfg, now=base)
    session.commit()
    cleared = auto_clear_resolved(
        session, current_anomaly_keys={a.key}, now=base + timedelta(seconds=70),
    )
    session.commit()

    assert cleared == []
    rows = session.query(StrikeRow).all()
    assert rows[0].resolved is False


def test_resolved_strike_unresolved_when_anomaly_returns(session):
    cfg = _cfg()
    a = _anomaly()
    base = datetime(2026, 5, 28, 14, 0, tzinfo=timezone.utc)

    process_anomaly(session, a, cfg, now=base)
    session.commit()
    auto_clear_resolved(session, current_anomaly_keys=set(), now=base + timedelta(seconds=70))
    session.commit()
    o = process_anomaly(session, a, cfg, now=base + timedelta(seconds=200))
    session.commit()

    assert o.strike_count == 1
    assert o.action == "logged_strike1"
    rows = session.query(StrikeRow).filter(StrikeRow.resolved == False).all()
    assert len(rows) == 1
