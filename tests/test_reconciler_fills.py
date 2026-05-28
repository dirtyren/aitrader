"""Tests for apply_tagged_fill — the read-side counterpart to Plan 2's COID minting."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from reconciler.fills import apply_tagged_fill
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
def store():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    s = MySQLStore.__new__(MySQLStore)
    s._engine = engine
    s.strategy_name = "reconciler"
    s._log = logging.getLogger("test_fills")
    with Session(engine) as session:
        session.add_all([
            StrategyRow(name="vwap_wave"),
            StrategyRow(name="rsi_equity"),
        ])
        session.commit()
        rows = session.query(StrategyRow).order_by(StrategyRow.id).all()
    s._strategy_id = rows[0].id
    return s


def _filled_order(coid: str | None, *, side: str = "buy", qty: str = "1",
                  filled_avg_price: str = "100.00", symbol: str = "AAPL",
                  asset_class: str = "us_equity",
                  filled_at: str = "2026-05-28T14:00:00Z") -> dict:
    return {
        "id": "alp-1",
        "client_order_id": coid,
        "side": side,
        "filled_qty": qty,
        "filled_avg_price": filled_avg_price,
        "symbol": symbol,
        "status": "filled",
        "asset_class": asset_class,
        "filled_at": filled_at,
    }


def _events(session: Session) -> list[str]:
    return [r.type for r in session.query(EventRow).order_by(EventRow.id).all()]


def _coid(strategy="vwap_wave", setup="vwap_bounce", symbol="AAPL",
          role="entry", uuid="abcd1234"):
    return f"aitrader__{strategy}__{setup}__{symbol}__{role}__{uuid}"


# ── untagged ───────────────────────────────────────────────────────────


def test_untagged_fill_writes_event_only(store):
    fill = _filled_order(coid=None)
    with Session(store._engine) as session:
        apply_tagged_fill(session, fill, store)
        session.commit()
    with Session(store._engine) as session:
        assert _events(session) == ["untagged_fill"]
        assert session.query(PositionRow).count() == 0
        assert session.query(TradeRow).count() == 0


def test_unparseable_coid_writes_event_only(store):
    fill = _filled_order(coid="not_an_aitrader_coid_xxxxxxxx")
    with Session(store._engine) as session:
        apply_tagged_fill(session, fill, store)
        session.commit()
    with Session(store._engine) as session:
        assert _events(session) == ["untagged_fill"]


# ── tagged entry, no MySQL row → recovery insert ──────────────────────


def test_tagged_entry_with_no_matching_row_inserts_position(store):
    coid = _coid(role="entry")
    fill = _filled_order(coid=coid)
    with Session(store._engine) as session:
        apply_tagged_fill(session, fill, store)
        session.commit()
    with Session(store._engine) as session:
        rows = session.query(PositionRow).all()
        assert len(rows) == 1
        assert rows[0].client_order_id == coid
        assert rows[0].setup_name == "vwap_bounce"
        assert rows[0].adopted is False
        assert _events(session) == ["tagged_entry_inserted"]


def test_tagged_entry_with_matching_row_is_idempotent(store):
    coid = _coid(role="entry")
    pos = OpenPosition(
        symbol="AAPL", setup="vwap_bounce", side="long", qty=1.0,
        entry_px=100.0, stop_px=99.0, target_px=101.0,
        opened_at=datetime(2026, 5, 28, 14, 0, tzinfo=timezone.utc),
        order_id="o1", initial_stop_px=99.0, client_order_id=coid,
    )
    store.position_opened(pos, "equity")
    fill = _filled_order(coid=coid)
    with Session(store._engine) as session:
        apply_tagged_fill(session, fill, store)
        session.commit()
    with Session(store._engine) as session:
        assert session.query(PositionRow).count() == 1
        assert _events(session) == []


# ── tagged exit ────────────────────────────────────────────────────────


def test_tagged_exit_closes_matching_position(store):
    entry_coid = _coid(role="entry")
    pos = OpenPosition(
        symbol="AAPL", setup="vwap_bounce", side="long", qty=1.0,
        entry_px=100.0, stop_px=99.0, target_px=101.0,
        opened_at=datetime(2026, 5, 28, 14, 0, tzinfo=timezone.utc),
        order_id="o1", initial_stop_px=99.0, client_order_id=entry_coid,
    )
    store.position_opened(pos, "equity")

    exit_coid = _coid(role="exit", uuid="deadbeef")
    fill = _filled_order(
        coid=exit_coid, side="sell", filled_avg_price="102.50",
    )
    with Session(store._engine) as session:
        apply_tagged_fill(session, fill, store)
        session.commit()
    with Session(store._engine) as session:
        pos_row = session.query(PositionRow).one()
        assert pos_row.status == "closed"
        assert pos_row.exit_client_order_id == exit_coid
        assert pos_row.close_reason == "broker_fill"
        trade = session.query(TradeRow).one()
        assert trade.exit_px == pytest.approx(102.50)
        assert trade.exit_client_order_id == exit_coid
        assert _events(session) == ["tagged_fill_applied"]


def test_tagged_exit_with_no_matching_open_row_is_idempotent(store):
    """Exit fill arrives but position is already closed (re-applied across cycles)."""
    fill = _filled_order(coid=_coid(role="exit"), side="sell")
    with Session(store._engine) as session:
        apply_tagged_fill(session, fill, store)
        session.commit()
    with Session(store._engine) as session:
        assert session.query(TradeRow).count() == 0
        # No event — true noop
        assert _events(session) == []


def test_tagged_target_role_closes_position_same_as_exit(store):
    """role=target (crypto TP fill) closes the position."""
    entry_coid = _coid(role="entry")
    pos = OpenPosition(
        symbol="BTCUSD", setup="vwap_bounce", side="long", qty=0.5,
        entry_px=50000.0, stop_px=49500.0, target_px=51000.0,
        opened_at=datetime(2026, 5, 28, 14, 0, tzinfo=timezone.utc),
        order_id="o1", initial_stop_px=49500.0, client_order_id=entry_coid,
    )
    store.position_opened(pos, "crypto")

    tp_coid = _coid(role="target", symbol="BTCUSD", uuid="cafebabe")
    fill = _filled_order(
        coid=tp_coid, side="sell", filled_avg_price="51000.00",
        symbol="BTCUSD", asset_class="crypto",
    )
    with Session(store._engine) as session:
        apply_tagged_fill(session, fill, store)
        session.commit()
    with Session(store._engine) as session:
        pos_row = session.query(PositionRow).one()
        assert pos_row.status == "closed"
        assert pos_row.close_reason == "broker_fill"


# ── unknown strategy ──────────────────────────────────────────────────


def test_tagged_fill_for_unknown_strategy_writes_untagged(store):
    """COID names a strategy that doesn't exist in MySQL → can't attribute."""
    coid = _coid(strategy="ghost_strategy", role="entry")
    fill = _filled_order(coid=coid)
    with Session(store._engine) as session:
        apply_tagged_fill(session, fill, store)
        session.commit()
    with Session(store._engine) as session:
        assert _events(session) == ["untagged_fill"]
        assert session.query(PositionRow).count() == 0
