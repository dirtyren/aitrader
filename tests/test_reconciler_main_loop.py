"""Integration test for one reconciler cycle."""
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
    PositionRow,
    StrategyRow,
    StrikeRow,
)
from state.position_book import OpenPosition


@pytest.fixture
def store():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    s = MySQLStore.__new__(MySQLStore)
    s._engine = engine
    s.strategy_name = "reconciler"
    s._log = logging.getLogger("test_loop")
    with Session(engine) as session:
        session.add(StrategyRow(name="vwap_wave"))
        session.commit()
        s._strategy_id = session.query(StrategyRow).one().id
    return s


def _cfg(shadow=False):
    return ReconcilerConfig(
        interval_s=30, strike_threshold=3, strike_min_gap_s=60,
        qty_eps=1e-6, shadow_mode=shadow,
        state_file_path="/tmp/state.json",
    )


def _coid(role="entry", uuid="abcd1234"):
    return f"aitrader__vwap_wave__vwap_bounce__AAPL__{role}__{uuid}"


def test_cycle_emits_heartbeat(store):
    alpaca = MagicMock()
    alpaca.get_positions.return_value = []
    alpaca.list_orders.return_value = []
    cfg = _cfg()
    now = datetime(2026, 5, 28, 14, 0, tzinfo=timezone.utc)

    advanced_to = run_one_cycle(
        store=store, alpaca=alpaca, cfg=cfg,
        last_orders_check_ts=None, now=now,
    )

    with Session(store._engine) as session:
        types = [r.type for r in session.query(EventRow).order_by(EventRow.id).all()]
        assert "heartbeat" in types
    assert advanced_to == now


def test_cycle_applies_tagged_entry_fill_and_inserts_position(store):
    alpaca = MagicMock()
    alpaca.get_positions.return_value = [{
        "symbol": "AAPL", "qty": "1", "side": "long",
        "asset_class": "us_equity",
    }]
    alpaca.list_orders.return_value = [{
        "id": "alp-1", "client_order_id": _coid(role="entry"),
        "side": "buy", "filled_qty": "1", "filled_avg_price": "100.00",
        "symbol": "AAPL", "asset_class": "us_equity",
        "filled_at": "2026-05-28T13:55:00Z", "status": "filled",
    }]
    cfg = _cfg()
    now = datetime(2026, 5, 28, 14, 0, tzinfo=timezone.utc)

    run_one_cycle(
        store=store, alpaca=alpaca, cfg=cfg,
        last_orders_check_ts=None, now=now,
    )

    with Session(store._engine) as session:
        positions = session.query(PositionRow).all()
        assert len(positions) == 1
        assert positions[0].client_order_id == _coid(role="entry")
        # No anomaly: invariant holds (mysql_sum=1, broker=1)
        strikes = session.query(StrikeRow).all()
        assert strikes == []


def test_cycle_strike_progression_alerts_on_threshold(store):
    """Three consecutive cycles with the same drift should alert at strike 3."""
    pos = OpenPosition(
        symbol="AAPL", setup="vwap_bounce", side="long", qty=2.0,
        entry_px=100.0, stop_px=99.0, target_px=101.0,
        opened_at=datetime(2026, 5, 28, 13, 0, tzinfo=timezone.utc),
        order_id="o", initial_stop_px=99.0,
        client_order_id=_coid(),
    )
    store.position_opened(pos, "equity")

    alpaca = MagicMock()
    alpaca.get_positions.return_value = [{
        "symbol": "AAPL", "qty": "1", "side": "long", "asset_class": "us_equity",
    }]
    alpaca.list_orders.return_value = []
    cfg = _cfg()

    base = datetime(2026, 5, 28, 14, 0, tzinfo=timezone.utc)
    for i in range(3):
        now = base + timedelta(seconds=cfg.strike_min_gap_s * i + i)
        run_one_cycle(
            store=store, alpaca=alpaca, cfg=cfg,
            last_orders_check_ts=None, now=now,
        )

    with Session(store._engine) as session:
        strike = session.query(StrikeRow).filter(
            StrikeRow.direction == "qty_drift",
        ).one()
        assert strike.strike_count == 3
        types = [
            r.type for r in session.query(EventRow).order_by(EventRow.id).all()
        ]
        assert "qty_drift_confirmed" in types


def test_shadow_mode_does_not_mutate_positions(store):
    """In shadow mode, fills are seen and events are written, but no INSERT."""
    alpaca = MagicMock()
    alpaca.get_positions.return_value = []
    alpaca.list_orders.return_value = [{
        "id": "alp-1", "client_order_id": _coid(role="entry"),
        "side": "buy", "filled_qty": "1", "filled_avg_price": "100.00",
        "symbol": "AAPL", "asset_class": "us_equity",
        "filled_at": "2026-05-28T13:55:00Z", "status": "filled",
    }]
    cfg = _cfg(shadow=True)
    now = datetime(2026, 5, 28, 14, 0, tzinfo=timezone.utc)

    run_one_cycle(
        store=store, alpaca=alpaca, cfg=cfg,
        last_orders_check_ts=None, now=now,
    )

    with Session(store._engine) as session:
        # No position inserted in shadow mode
        assert session.query(PositionRow).count() == 0
        # But events ARE written (visibility for the operator)
        types = [r.type for r in session.query(EventRow).all()]
        assert "heartbeat" in types
        assert "shadow_would_apply_fill" in types


def test_cycle_skips_on_alpaca_error(store):
    alpaca = MagicMock()
    alpaca.get_positions.side_effect = Exception("API down")
    alpaca.list_orders.return_value = []
    cfg = _cfg()
    now = datetime(2026, 5, 28, 14, 0, tzinfo=timezone.utc)

    advanced_to = run_one_cycle(
        store=store, alpaca=alpaca, cfg=cfg,
        last_orders_check_ts=None, now=now,
    )

    # Cycle skipped — last_orders_check_ts is NOT advanced
    assert advanced_to is None
