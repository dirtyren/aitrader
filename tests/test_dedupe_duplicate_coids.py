"""Tests for scripts/dedupe_duplicate_coids.py — the one-shot script that
closes duplicate open positions sharing a client_order_id.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from scripts import dedupe_duplicate_coids
from state.mysql_store import (
    Base,
    EventRow,
    MySQLStore,
    PositionRow,
    StrategyRow,
    TradeRow,
)
from state.position_book import OpenPosition


@pytest.fixture
def store(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    s = MySQLStore.__new__(MySQLStore)
    s._engine = engine
    s.strategy_name = "operator"
    s._log = logging.getLogger("test_dedupe")
    with Session(engine) as session:
        session.add(StrategyRow(name="operator"))
        session.add(StrategyRow(name="vwap_wave"))
        session.commit()
        s._strategy_id = session.query(StrategyRow).filter_by(
            name="operator"
        ).one().id

    # Make the script use this fixture's store instead of building a real one.
    def _fake_init(self, *, strategy_name=None):
        self.__dict__.update(s.__dict__)
        self.strategy_name = strategy_name or self.strategy_name
    monkeypatch.setattr(MySQLStore, "__init__", _fake_init)
    monkeypatch.setattr(MySQLStore, "ensure_schema", lambda self: None)
    return s


def _open_pos(store, symbol, setup, coid, opened_at, qty=1.0):
    pos = OpenPosition(
        symbol=symbol, setup=setup, side="long", qty=qty,
        entry_px=100.0, stop_px=99.0, target_px=101.0,
        opened_at=opened_at,
        order_id="o", initial_stop_px=99.0,
        client_order_id=coid,
    )
    # Override strategy_id so the inserted row attaches to vwap_wave, not operator.
    saved = store._strategy_id
    with Session(store._engine) as session:
        sid = session.query(StrategyRow).filter_by(name="vwap_wave").one().id
    store._strategy_id = sid
    try:
        store.position_opened(pos, "equity")
    finally:
        store._strategy_id = saved


def test_dry_run_does_not_modify_db(store):
    coid = "aitrader__vwap_wave__vwap_bounce__AAPL__entry__abcd1234"
    _open_pos(store, "AAPL", "vwap_bounce", coid,
              datetime(2026, 5, 28, 14, 0, tzinfo=timezone.utc))
    _open_pos(store, "AAPL", "vwap_bounce", coid,
              datetime(2026, 5, 28, 14, 5, tzinfo=timezone.utc))

    rc = dedupe_duplicate_coids.main(["--dry-run"])
    assert rc == 0
    with Session(store._engine) as session:
        # Both rows still open, no trades, no events.
        assert session.query(PositionRow).filter(
            PositionRow.status == "open").count() == 2
        assert session.query(TradeRow).count() == 0
        assert session.query(EventRow).count() == 0


def test_apply_keeps_oldest_closes_others(store):
    coid = "aitrader__vwap_wave__vwap_bounce__AAPL__entry__abcd1234"
    older_at = datetime(2026, 5, 28, 14, 0, tzinfo=timezone.utc)
    newer_at = datetime(2026, 5, 28, 14, 5, tzinfo=timezone.utc)
    _open_pos(store, "AAPL", "vwap_bounce", coid, older_at)
    _open_pos(store, "AAPL", "vwap_bounce", coid, newer_at)

    rc = dedupe_duplicate_coids.main(["--apply"])
    assert rc == 0

    with Session(store._engine) as session:
        open_rows = session.query(PositionRow).filter(
            PositionRow.status == "open").all()
        closed_rows = session.query(PositionRow).filter(
            PositionRow.status == "closed").all()
        assert len(open_rows) == 1
        assert len(closed_rows) == 1
        # Oldest kept (compare naive — SQLite drops tzinfo).
        assert open_rows[0].opened_at.replace(tzinfo=None) == older_at.replace(tzinfo=None)
        assert closed_rows[0].close_reason == "duplicate_dedupe"

        trades = session.query(TradeRow).all()
        assert len(trades) == 1
        assert trades[0].close_reason == "duplicate_dedupe"
        assert trades[0].pnl_usd == 0
        assert trades[0].client_order_id == coid

        events = session.query(EventRow).all()
        assert len(events) == 1
        assert events[0].type == "duplicate_dedupe_applied"
        assert events[0].payload["client_order_id"] == coid
        assert events[0].payload["kept_position_id"] == open_rows[0].id
        assert events[0].payload["closed_position_id"] == closed_rows[0].id


def test_apply_is_idempotent(store):
    coid = "aitrader__vwap_wave__vwap_bounce__AAPL__entry__abcd1234"
    _open_pos(store, "AAPL", "vwap_bounce", coid,
              datetime(2026, 5, 28, 14, 0, tzinfo=timezone.utc))
    _open_pos(store, "AAPL", "vwap_bounce", coid,
              datetime(2026, 5, 28, 14, 5, tzinfo=timezone.utc))

    assert dedupe_duplicate_coids.main(["--apply"]) == 0
    # Second run finds nothing — exits cleanly with no DB changes.
    with Session(store._engine) as session:
        events_before = session.query(EventRow).count()
    assert dedupe_duplicate_coids.main(["--apply"]) == 0
    with Session(store._engine) as session:
        assert session.query(EventRow).count() == events_before


def test_no_duplicates_is_noop(store):
    coid = "aitrader__vwap_wave__vwap_bounce__AAPL__entry__abcd1234"
    _open_pos(store, "AAPL", "vwap_bounce", coid,
              datetime(2026, 5, 28, 14, 0, tzinfo=timezone.utc))

    assert dedupe_duplicate_coids.main(["--dry-run"]) == 0
    assert dedupe_duplicate_coids.main(["--apply"]) == 0
    with Session(store._engine) as session:
        assert session.query(PositionRow).filter(
            PositionRow.status == "open").count() == 1
        assert session.query(TradeRow).count() == 0
        assert session.query(EventRow).count() == 0
