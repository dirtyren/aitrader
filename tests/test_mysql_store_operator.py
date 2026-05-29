"""Tests for MySQLStore operator-CLI helpers (Plan 4).

Uses an in-memory SQLite engine to verify the read/resolve/insert paths
the CLI relies on.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from state.mysql_store import (
    Base,
    EventRow,
    MySQLStore,
    PositionRow,
    StrategyRow,
    StrikeRow,
)


@pytest.fixture
def store():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    s = MySQLStore.__new__(MySQLStore)
    s._engine = engine
    s.strategy_name = "operator"
    s._log = logging.getLogger("test_operator")
    with Session(engine) as session:
        session.add_all([
            StrategyRow(name="vwap_wave"),
            StrategyRow(name="rsi_equity"),
        ])
        session.commit()
        rows = session.query(StrategyRow).order_by(StrategyRow.id).all()
    s._strategy_id = rows[0].id
    s._other_strategy_id = rows[1].id
    return s


def _strike(store, *, key="qty_drift:AAPL", direction="qty_drift",
            symbol="AAPL", strategy_id=None, count=3, resolved=False):
    base = datetime(2026, 5, 28, 14, 0, tzinfo=timezone.utc)
    with Session(store._engine) as session:
        row = StrikeRow(
            key=key,
            direction=direction,
            strategy_id=strategy_id,
            symbol=symbol,
            strike_count=count,
            first_seen_at=base,
            last_seen_at=base + timedelta(seconds=120),
            last_observed_state={"mysql_sum": 2.0, "broker_qty": 1.0},
            resolved=resolved,
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        return row.id


# ── list_unresolved_strikes ──────────────────────────────────────────


def test_list_unresolved_strikes_returns_only_unresolved(store):
    _strike(store, key="qty_drift:AAPL", resolved=False)
    _strike(store, key="qty_drift:OLD", resolved=True)
    rows = store.list_unresolved_strikes()
    assert len(rows) == 1
    assert rows[0].key == "qty_drift:AAPL"


def test_list_unresolved_strikes_orders_by_last_seen_desc(store):
    base = datetime(2026, 5, 28, 14, 0, tzinfo=timezone.utc)
    with Session(store._engine) as session:
        session.add(StrikeRow(
            key="qty_drift:OLD", direction="qty_drift", symbol="X",
            strike_count=1, first_seen_at=base, last_seen_at=base,
            last_observed_state={}, resolved=False,
        ))
        session.add(StrikeRow(
            key="qty_drift:NEW", direction="qty_drift", symbol="Y",
            strike_count=1, first_seen_at=base, last_seen_at=base + timedelta(seconds=300),
            last_observed_state={}, resolved=False,
        ))
        session.commit()
    rows = store.list_unresolved_strikes()
    assert [r.key for r in rows] == ["qty_drift:NEW", "qty_drift:OLD"]


# ── get_strike_by_id ─────────────────────────────────────────────────


def test_get_strike_by_id_returns_row(store):
    sid = _strike(store)
    row = store.get_strike_by_id(sid)
    assert row is not None
    assert row.id == sid
    assert row.key == "qty_drift:AAPL"


def test_get_strike_by_id_returns_none_for_unknown(store):
    assert store.get_strike_by_id(99999) is None


# ── resolve_strike ───────────────────────────────────────────────────


def test_resolve_strike_marks_resolved_and_writes_event(store):
    sid = _strike(store)
    ok = store.resolve_strike(sid, reason="operator_closed_position",
                              operator_note="manual close on broker")
    assert ok is True
    with Session(store._engine) as session:
        row = session.query(StrikeRow).one()
        assert row.resolved is True
        assert row.resolved_reason == "operator_closed_position"
        assert row.resolved_at is not None
        events = session.query(EventRow).all()
        assert any(e.type == "operator_action" for e in events)


def test_resolve_strike_returns_false_when_already_resolved(store):
    sid = _strike(store, resolved=True)
    ok = store.resolve_strike(sid, reason="operator_dismissed",
                              operator_note="manual")
    assert ok is False


def test_resolve_strike_returns_false_when_unknown(store):
    ok = store.resolve_strike(99999, reason="operator_dismissed",
                              operator_note="manual")
    assert ok is False


# ── recent_events ────────────────────────────────────────────────────


def test_recent_events_returns_newest_first(store):
    with Session(store._engine) as session:
        for i in range(3):
            session.add(EventRow(
                type=f"e{i}",
                created_at=datetime(2026, 5, 28, 14, i, tzinfo=timezone.utc),
            ))
        session.commit()
    rows = store.recent_events(limit=10)
    types = [r.type for r in rows]
    assert types == ["e2", "e1", "e0"]


def test_recent_events_respects_limit(store):
    with Session(store._engine) as session:
        for i in range(5):
            session.add(EventRow(type=f"e{i}",
                                 created_at=datetime(2026, 5, 28, 14, i, tzinfo=timezone.utc)))
        session.commit()
    rows = store.recent_events(limit=2)
    assert len(rows) == 2


# ── events_for_strike ────────────────────────────────────────────────


def test_events_for_strike_filters_by_symbol_and_strategy(store):
    sid = _strike(store, key="mysql_only:1:AAPL", direction="mysql_only",
                  symbol="AAPL", strategy_id=store._strategy_id)
    strike = store.get_strike_by_id(sid)

    base = datetime(2026, 5, 28, 14, 0, tzinfo=timezone.utc)
    with Session(store._engine) as session:
        session.add(EventRow(type="mysql_only_confirmed",
                             strategy_id=store._strategy_id, symbol="AAPL",
                             created_at=base))
        session.add(EventRow(type="mysql_only_confirmed",
                             strategy_id=store._other_strategy_id, symbol="AAPL",
                             created_at=base))
        session.add(EventRow(type="heartbeat", symbol="MSFT",
                             created_at=base))
        session.commit()

    events = store.events_for_strike(strike, limit=20)
    types = [(e.type, e.strategy_id, e.symbol) for e in events]
    assert (("mysql_only_confirmed", store._strategy_id, "AAPL")
            in types)
    assert all(e.symbol == "AAPL" for e in events)
    assert all(e.strategy_id in (store._strategy_id, None) for e in events)


def test_events_for_strike_no_strategy_id_filter_for_qty_drift(store):
    """qty_drift strikes have strategy_id=None; events match by symbol only."""
    sid = _strike(store, key="qty_drift:AAPL", direction="qty_drift",
                  symbol="AAPL", strategy_id=None)
    strike = store.get_strike_by_id(sid)

    base = datetime(2026, 5, 28, 14, 0, tzinfo=timezone.utc)
    with Session(store._engine) as session:
        session.add(EventRow(type="qty_drift_confirmed",
                             strategy_id=store._strategy_id, symbol="AAPL",
                             created_at=base))
        session.add(EventRow(type="qty_drift_confirmed",
                             strategy_id=store._other_strategy_id, symbol="AAPL",
                             created_at=base))
        session.commit()

    events = store.events_for_strike(strike, limit=20)
    assert len(events) == 2


# ── insert_adopted_position ──────────────────────────────────────────


def test_insert_adopted_position_creates_adopted_row(store):
    coid = "aitrader__vwap_wave__adopted__SOLUSD__adopted__abadcafe"
    new_id = store.insert_adopted_position(
        strategy_id=store._strategy_id,
        setup_name="adopted",
        symbol="SOLUSD",
        side="long",
        qty=10.0,
        entry_px=100.0,
        opened_at=datetime(2026, 5, 28, 14, 0, tzinfo=timezone.utc),
        asset_class="crypto",
        client_order_id=coid,
    )
    assert isinstance(new_id, int)
    with Session(store._engine) as session:
        row = session.query(PositionRow).one()
        assert row.adopted is True
        assert row.client_order_id == coid
        assert row.symbol == "SOLUSD"
        assert row.status == "open"
