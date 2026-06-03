"""Tests for COID round-trip in MySQLStore (Plan 2).

Uses an in-memory SQLite engine to write and read positions and trades
with client_order_id / exit_client_order_id values. Bypasses _build_url
by constructing the engine directly.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from state.mysql_store import (
    Base,
    MySQLStore,
    PositionRow,
    StrategyRow,
    TradeRow,
)
from state.position_book import OpenPosition


@pytest.fixture
def store():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    s = MySQLStore.__new__(MySQLStore)
    s._engine = engine
    s.strategy_name = "vwap_wave"
    s._log = logging.getLogger("test_coid")
    # Pre-create the strategy row so foreign keys resolve
    with Session(engine) as session:
        session.add(StrategyRow(name="vwap_wave"))
        session.commit()
        s._strategy_id = session.query(StrategyRow.id).filter_by(name="vwap_wave").one()[0]
    return s


def _pos(coid: str | None = "aitrader__vwap_wave__vwap_bounce__AAPL__entry__abcd1234"):
    return OpenPosition(
        symbol="AAPL", setup="vwap_bounce", side="long", qty=1.0,
        entry_px=100.0, stop_px=99.0, target_px=101.0,
        opened_at=datetime(2026, 5, 28, 14, 0, tzinfo=timezone.utc),
        order_id="o1",
        initial_stop_px=99.0,
        client_order_id=coid,
    )


def test_position_opened_persists_client_order_id(store):
    store.position_opened(_pos(), "equity")
    with Session(store._engine) as session:
        row = session.query(PositionRow).one()
        assert row.client_order_id == "aitrader__vwap_wave__vwap_bounce__AAPL__entry__abcd1234"


def test_load_open_positions_round_trips_client_order_id(store):
    store.position_opened(_pos(), "equity")
    book = store.load_open_positions()
    pos = book.get("AAPL", "vwap_bounce")
    assert pos is not None
    assert pos.client_order_id == "aitrader__vwap_wave__vwap_bounce__AAPL__entry__abcd1234"


def test_position_closed_writes_exit_client_order_id_to_positions_and_trades(store):
    store.position_opened(_pos(), "equity")
    exit_coid = "aitrader__vwap_wave__vwap_bounce__AAPL__exit__deadbeef"
    result = store.position_closed(
        symbol="AAPL",
        exit_px=102.0,
        close_reason="target",
        setup_name="vwap_bounce",
        exit_client_order_id=exit_coid,
    )
    assert result is not None
    with Session(store._engine) as session:
        pos_row = session.query(PositionRow).one()
        assert pos_row.status == "closed"
        assert pos_row.exit_client_order_id == exit_coid
        # Entry COID preserved on the closed positions row
        assert pos_row.client_order_id == "aitrader__vwap_wave__vwap_bounce__AAPL__entry__abcd1234"

        trade_row = session.query(TradeRow).one()
        assert trade_row.exit_client_order_id == exit_coid
        assert trade_row.client_order_id == "aitrader__vwap_wave__vwap_bounce__AAPL__entry__abcd1234"


def test_position_opened_without_coid_persists_null(store):
    """Backwards-compat: pre-Plan-2 callers (until rollout completes) skip COID."""
    store.position_opened(_pos(coid=None), "equity")
    with Session(store._engine) as session:
        row = session.query(PositionRow).one()
        assert row.client_order_id is None


def test_position_closed_without_exit_coid_persists_null(store):
    store.position_opened(_pos(), "equity")
    store.position_closed(
        symbol="AAPL", exit_px=102.0, close_reason="target",
        setup_name="vwap_bounce",
    )
    with Session(store._engine) as session:
        pos_row = session.query(PositionRow).one()
        assert pos_row.exit_client_order_id is None
        trade_row = session.query(TradeRow).one()
        assert trade_row.exit_client_order_id is None


def test_mark_exit_submitted_flips_flag(store):
    store.position_opened(_pos(), "equity")
    ok = store.mark_exit_submitted(
        strategy_id=store._strategy_id,
        symbol="AAPL",
        setup_name="vwap_bounce",
    )
    assert ok is True
    book = store.load_open_positions()
    reloaded = book.get("AAPL", "vwap_bounce")
    assert reloaded is not None
    assert reloaded.exit_submitted is True


def test_mark_exit_submitted_idempotent(store):
    store.position_opened(_pos(), "equity")
    store.mark_exit_submitted(
        strategy_id=store._strategy_id,
        symbol="AAPL", setup_name="vwap_bounce",
    )
    ok = store.mark_exit_submitted(
        strategy_id=store._strategy_id,
        symbol="AAPL", setup_name="vwap_bounce",
    )
    assert ok is True


def test_mark_exit_submitted_no_open_row_returns_false(store):
    ok = store.mark_exit_submitted(
        strategy_id=store._strategy_id,
        symbol="ZZZZ", setup_name="nonexistent",
    )
    assert ok is False


def test_load_open_positions_round_trips_exit_submitted(store):
    store.position_opened(_pos(), "equity")
    store.mark_exit_submitted(
        strategy_id=store._strategy_id,
        symbol="AAPL", setup_name="vwap_bounce",
    )
    book = store.load_open_positions()
    reloaded = book.get("AAPL", "vwap_bounce")
    assert reloaded.exit_submitted is True


def test_position_opened_default_exit_submitted_false(store):
    store.position_opened(_pos(), "equity")
    book = store.load_open_positions()
    pos = book.get("AAPL", "vwap_bounce")
    assert pos.exit_submitted is False
