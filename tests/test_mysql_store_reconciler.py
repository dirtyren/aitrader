"""Tests for MySQLStore helpers added for the reconciler service (Plan 3).

Uses an in-memory SQLite engine to verify cross-strategy queries and the
fill-recovery insert path.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from state.mysql_store import (
    Base,
    MySQLStore,
    PositionRow,
    StrategyRow,
)
from state.position_book import OpenPosition


@pytest.fixture
def store():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    s = MySQLStore.__new__(MySQLStore)
    s._engine = engine
    s.strategy_name = "vwap_wave"
    s._log = logging.getLogger("test_recon_helpers")
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


def _open_pos(store, strategy_id: int, symbol: str, setup: str, qty: float,
              client_order_id: str | None = None):
    pos = OpenPosition(
        symbol=symbol, setup=setup, side="long", qty=qty,
        entry_px=100.0, stop_px=99.0, target_px=101.0,
        opened_at=datetime(2026, 5, 28, 14, 0, tzinfo=timezone.utc),
        order_id="o", initial_stop_px=99.0,
        client_order_id=client_order_id,
    )
    saved_strategy_id = store._strategy_id
    store._strategy_id = strategy_id
    try:
        store.position_opened(pos, "equity")
    finally:
        store._strategy_id = saved_strategy_id


def test_find_open_position_by_coid_returns_row(store):
    coid = "aitrader__vwap_wave__vwap_bounce__AAPL__entry__abcd1234"
    _open_pos(store, store._strategy_id, "AAPL", "vwap_bounce", 1.0, coid)
    row = store.find_open_position_by_coid(coid)
    assert row is not None
    assert row.symbol == "AAPL"
    assert row.client_order_id == coid


def test_find_open_position_by_coid_returns_none_for_unknown(store):
    assert store.find_open_position_by_coid("aitrader__x__y__Z__entry__deadbeef") is None


def test_find_open_position_by_coid_returns_none_for_closed(store):
    coid = "aitrader__vwap_wave__vwap_bounce__AAPL__entry__abcd1234"
    _open_pos(store, store._strategy_id, "AAPL", "vwap_bounce", 1.0, coid)
    store.position_closed(symbol="AAPL", exit_px=101.0, close_reason="target",
                          setup_name="vwap_bounce")
    assert store.find_open_position_by_coid(coid) is None


def test_find_open_position_by_setup_cross_strategy(store):
    """Reconciler must look up positions in any strategy, not just self.strategy_id."""
    _open_pos(store, store._other_strategy_id, "AAPL", "rsi_long", 5.0)
    row = store.find_open_position_by_setup(
        strategy_id=store._other_strategy_id, symbol="AAPL", setup_name="rsi_long",
    )
    assert row is not None
    assert row.qty == Decimal("5.00000000")


def test_find_open_position_by_setup_returns_none_for_other_strategy(store):
    """Wrong strategy_id must not match."""
    _open_pos(store, store._strategy_id, "AAPL", "vwap_bounce", 1.0)
    row = store.find_open_position_by_setup(
        strategy_id=store._other_strategy_id, symbol="AAPL", setup_name="vwap_bounce",
    )
    assert row is None


def test_insert_position_from_fill_creates_row_with_coid(store):
    coid = "aitrader__vwap_wave__vwap_bounce__AAPL__entry__abcd1234"
    new_id = store.insert_position_from_fill(
        strategy_id=store._strategy_id,
        setup_name="vwap_bounce",
        symbol="AAPL",
        side="long",
        qty=2.0,
        entry_px=100.0,
        opened_at=datetime(2026, 5, 28, 14, 0, tzinfo=timezone.utc),
        asset_class="equity",
        client_order_id=coid,
    )
    assert isinstance(new_id, int)
    with Session(store._engine) as session:
        row = session.query(PositionRow).filter(PositionRow.id == new_id).one()
        assert row.client_order_id == coid
        assert row.qty == Decimal("2.00000000")
        assert row.status == "open"
        assert row.adopted is False  # crash-before-write recovery is not "adopted"


def test_sum_qty_by_symbol_aggregates_across_strategies(store):
    _open_pos(store, store._strategy_id,        "AAPL", "vwap_bounce", 3.0)
    _open_pos(store, store._other_strategy_id,  "AAPL", "rsi_long",    2.0)
    _open_pos(store, store._strategy_id,        "BTCUSD", "vwap_bounce", 0.5)
    sums = store.sum_qty_by_symbol()
    assert sums["AAPL"] == 5.0
    assert sums["BTCUSD"] == 0.5


def test_sum_qty_by_symbol_normalizes_crypto_slash(store):
    """A position stored as BTC/USD aggregates with one stored as BTCUSD."""
    _open_pos(store, store._strategy_id,       "BTC/USD", "vwap_bounce", 0.5)
    _open_pos(store, store._other_strategy_id, "BTCUSD",  "rsi_long",    0.3)
    sums = store.sum_qty_by_symbol()
    # Both entries collapse under the broker-flat key.
    assert sums["BTCUSD"] == pytest.approx(0.8)


def test_position_closed_accepts_strategy_id_override(store):
    """The reconciler must close positions across strategies by id, not by name."""
    _open_pos(store, store._other_strategy_id, "AAPL", "rsi_long", 5.0)
    result = store.position_closed(
        symbol="AAPL",
        exit_px=101.0,
        close_reason="broker_fill",
        setup_name="rsi_long",
        strategy_id=store._other_strategy_id,
    )
    assert result is not None
    with Session(store._engine) as session:
        row = session.query(PositionRow).filter(PositionRow.symbol == "AAPL").one()
        assert row.status == "closed"
