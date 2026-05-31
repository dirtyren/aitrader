"""Strategy disable sweep — close every open position via the broker."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from broker.alpaca_client import InsufficientBuyingPowerError
from broker.client_order_id import parse_client_order_id
from state.mysql_store import (
    Base,
    MySQLStore,
    PositionRow,
    StrategyRow,
)
from state.strategy_close_all import close_all_open_positions


@pytest.fixture
def store():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    s = MySQLStore.__new__(MySQLStore)
    s._engine = engine
    s.strategy_name = "vwap_wave"
    s._strategy_id = None
    s._log = logging.getLogger("test_close_all")
    with Session(engine) as session:
        session.add(StrategyRow(name="vwap_wave"))
        session.commit()
        s._strategy_id = session.query(StrategyRow).one().id
    return s


def _seed_position(session: Session, *, strategy_id: int, symbol: str,
                   setup: str, qty: float = 0.5, side: str = "long",
                   asset_class: str = "crypto") -> None:
    session.add(PositionRow(
        strategy_id=strategy_id, symbol=symbol, asset_class=asset_class,
        side=side, qty=Decimal(str(qty)),
        entry_px=Decimal("100"), stop_px=None, target_px=None,
        initial_stop_px=None, setup_name=setup, order_id="x",
        client_order_id="aitrader__vwap_wave__" + setup + "__"
                        + symbol.replace("/", "") + "__entry__abcdef12",
        stop_order_id=None, breakeven_moved=False, bars_held=0,
        adopted=False, status="open",
        opened_at=datetime(2026, 5, 30, 14, 0, tzinfo=timezone.utc),
    ))


def _alpaca_with(submit_responses):
    """Helper: MagicMock alpaca where submit_order returns each item in turn."""
    alpaca = MagicMock()
    alpaca.list_orders.return_value = []
    alpaca.submit_order.side_effect = submit_responses
    alpaca.get_positions.return_value = []
    return alpaca


def test_all_positions_close_cleanly(store):
    with Session(store._engine) as session:
        _seed_position(session, strategy_id=store.strategy_id,
                       symbol="BTC/USD", setup="price_discovery")
        _seed_position(session, strategy_id=store.strategy_id,
                       symbol="ETH/USD", setup="fade_extreme")
        _seed_position(session, strategy_id=store.strategy_id,
                       symbol="SOL/USD", setup="price_discovery")
        session.commit()

    alpaca = _alpaca_with([
        {"id": "ord-1"}, {"id": "ord-2"}, {"id": "ord-3"},
    ])

    result = close_all_open_positions(
        alpaca=alpaca, mysql=store,
        strategy_name="vwap_wave", reason="operator_disable",
    )

    assert result.total == 3
    assert len(result.closed) == 3
    assert result.failed == []

    with Session(store._engine) as session:
        rows = session.query(PositionRow).all()
        assert all(r.status == "closed" for r in rows)
        assert all(r.close_reason == "operator_disable" for r in rows)


def test_partial_failure_leaves_failed_rows_open(store):
    with Session(store._engine) as session:
        _seed_position(session, strategy_id=store.strategy_id,
                       symbol="BTC/USD", setup="price_discovery")
        _seed_position(session, strategy_id=store.strategy_id,
                       symbol="ETH/USD", setup="fade_extreme")
        _seed_position(session, strategy_id=store.strategy_id,
                       symbol="SOL/USD", setup="price_discovery")
        session.commit()

    # Middle submit raises a non-qty 4xx; the broker-truth retry can't help
    # (no broker_pos), so safe_close returns None and we record a failure.
    alpaca = MagicMock()
    alpaca.list_orders.return_value = []
    alpaca.get_positions.return_value = []
    alpaca.submit_order.side_effect = [
        {"id": "ord-1"},
        InsufficientBuyingPowerError(403, "insufficient day trading buying power"),
        {"id": "ord-3"},
    ]

    result = close_all_open_positions(
        alpaca=alpaca, mysql=store,
        strategy_name="vwap_wave", reason="operator_disable",
    )

    assert result.total == 3
    assert len(result.closed) == 2
    assert len(result.failed) == 1
    assert result.failed[0][0] == "ETH/USD"

    with Session(store._engine) as session:
        eth = session.query(PositionRow).filter(
            PositionRow.symbol == "ETH/USD",
        ).one()
        assert eth.status == "open"


def test_empty_book_is_noop(store):
    alpaca = MagicMock()
    result = close_all_open_positions(
        alpaca=alpaca, mysql=store,
        strategy_name="vwap_wave", reason="operator_disable",
    )
    assert result.total == 0
    assert result.closed == []
    assert result.failed == []
    alpaca.submit_order.assert_not_called()


def test_cancels_open_orders_before_close(store):
    with Session(store._engine) as session:
        _seed_position(session, strategy_id=store.strategy_id,
                       symbol="BTC/USD", setup="price_discovery")
        session.commit()

    alpaca = MagicMock()
    alpaca.list_orders.return_value = [
        {"id": "bracket-tp-1"}, {"id": "bracket-stop-1"},
    ]
    alpaca.get_positions.return_value = []
    alpaca.submit_order.return_value = {"id": "ord-close"}

    close_all_open_positions(
        alpaca=alpaca, mysql=store,
        strategy_name="vwap_wave", reason="operator_disable",
    )

    cancel_calls = [c.args[0] for c in alpaca.cancel_order.call_args_list]
    assert "bracket-tp-1" in cancel_calls
    assert "bracket-stop-1" in cancel_calls


def test_exit_coid_is_operator_disable_format(store):
    with Session(store._engine) as session:
        _seed_position(session, strategy_id=store.strategy_id,
                       symbol="BTC/USD", setup="price_discovery")
        session.commit()

    alpaca = _alpaca_with([{"id": "ord-1"}])

    close_all_open_positions(
        alpaca=alpaca, mysql=store,
        strategy_name="vwap_wave", reason="operator_disable",
    )

    coid = alpaca.submit_order.call_args.kwargs["client_order_id"]
    parsed = parse_client_order_id(coid)
    assert parsed is not None
    assert parsed["strategy"] == "operator"
    assert parsed["setup"] == "disable"
    assert parsed["symbol"] == "BTCUSD"
    assert parsed["role"] == "exit"
