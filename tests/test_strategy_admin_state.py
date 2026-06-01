"""StrategyRow enable/disable plumbing — schema, store methods, admin view."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from state.mysql_store import (
    Base,
    EventRow,
    MySQLStore,
    PositionRow,
    StrategyRow,
    TradeRow,
)


@pytest.fixture
def store():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    s = MySQLStore.__new__(MySQLStore)
    s._engine = engine
    s.strategy_name = "vwap_wave"
    s._strategy_id = None
    s._log = logging.getLogger("test_admin_state")
    with Session(engine) as session:
        session.add(StrategyRow(name="vwap_wave"))
        session.add(StrategyRow(name="orb"))
        session.commit()
        s._strategy_id = session.query(StrategyRow).filter(
            StrategyRow.name == "vwap_wave",
        ).one().id
    return s


def _strategy_id(store: MySQLStore, name: str) -> int:
    with Session(store._engine) as session:
        return session.query(StrategyRow).filter(
            StrategyRow.name == name,
        ).one().id


def test_default_state_is_enabled(store):
    assert store.is_strategy_enabled() is True
    assert store.get_strategy_state(store.strategy_id) == "enabled"


def test_set_strategy_state_writes_event(store):
    store.set_strategy_state(
        strategy_id=store.strategy_id, enabled=False, state="disabling",
        reason="operator clicked Disable",
    )
    assert store.is_strategy_enabled() is False
    assert store.get_strategy_state(store.strategy_id) == "disabling"

    with Session(store._engine) as session:
        evts = session.query(EventRow).filter(
            EventRow.type == "strategy_state_changed",
        ).all()
        assert len(evts) == 1
        payload = evts[0].payload
        assert payload["from_state"] == "enabled"
        assert payload["to_state"] == "disabling"
        assert payload["from_enabled"] is True
        assert payload["to_enabled"] is False
        assert "operator clicked" in payload["reason"]


def test_set_strategy_state_rejects_unknown_state(store):
    with pytest.raises(ValueError, match="invalid strategy state"):
        store.set_strategy_state(
            strategy_id=store.strategy_id, enabled=False, state="frozen",
            reason="x",
        )


def test_get_strategies_admin_view_aggregates_correctly(store):
    """Seed positions + trades and check the rollup numbers."""
    sid_v = _strategy_id(store, "vwap_wave")
    sid_o = _strategy_id(store, "orb")

    today = datetime.now(timezone.utc).replace(microsecond=0)
    yesterday = today - timedelta(days=1)

    with Session(store._engine) as session:
        # vwap_wave: 2 open positions, 3 closed trades (2 wins, 1 loss)
        session.add(PositionRow(
            strategy_id=sid_v, symbol="BTCUSD", asset_class="crypto",
            side="long", qty=Decimal("0.5"), entry_px=Decimal("100"),
            stop_px=None, target_px=None, initial_stop_px=None,
            setup_name="price_discovery", order_id="x",
            stop_order_id=None, breakeven_moved=False, bars_held=0,
            adopted=False, status="open", opened_at=today,
        ))
        session.add(PositionRow(
            strategy_id=sid_v, symbol="ETHUSD", asset_class="crypto",
            side="long", qty=Decimal("1"), entry_px=Decimal("50"),
            stop_px=None, target_px=None, initial_stop_px=None,
            setup_name="fade", order_id="x",
            stop_order_id=None, breakeven_moved=False, bars_held=0,
            adopted=False, status="open", opened_at=today,
        ))
        session.add(TradeRow(
            strategy_id=sid_v, symbol="BTCUSD", asset_class="crypto",
            setup_name="price_discovery", side="long",
            qty=Decimal("0.5"), entry_px=Decimal("100"),
            exit_px=Decimal("110"), pnl_usd=Decimal("5"),
            R_realized=Decimal("1"), close_reason="target",
            opened_at=today, closed_at=today, bars_held=10,
        ))
        session.add(TradeRow(
            strategy_id=sid_v, symbol="ETHUSD", asset_class="crypto",
            setup_name="fade", side="long",
            qty=Decimal("1"), entry_px=Decimal("50"),
            exit_px=Decimal("55"), pnl_usd=Decimal("5"),
            R_realized=Decimal("1"), close_reason="target",
            opened_at=yesterday, closed_at=yesterday, bars_held=8,
        ))
        session.add(TradeRow(
            strategy_id=sid_v, symbol="SOLUSD", asset_class="crypto",
            setup_name="fade", side="long",
            qty=Decimal("10"), entry_px=Decimal("20"),
            exit_px=Decimal("19"), pnl_usd=Decimal("-10"),
            R_realized=Decimal("-1"), close_reason="stop",
            opened_at=yesterday, closed_at=yesterday, bars_held=5,
        ))
        session.commit()

    rows = store.get_strategies_admin_view()
    by_name = {r["name"]: r for r in rows}

    vw = by_name["vwap_wave"]
    assert vw["state"] == "enabled"
    assert vw["enabled"] is True
    assert vw["open_count"] == 2
    # today's trade only — BTCUSD pnl=+5
    assert vw["today_pnl"] == pytest.approx(5.0)
    # all trades sum to 5+5-10 = 0
    assert vw["total_pnl"] == pytest.approx(0.0)
    # 2 wins / 3 trades
    assert vw["win_rate"] == pytest.approx(2/3)
    assert vw["trade_count"] == 3

    orb = by_name["orb"]
    assert orb["open_count"] == 0
    assert orb["trade_count"] == 0
    assert orb["win_rate"] == 0.0


def test_is_strategy_enabled_resilient_on_db_failure(store, monkeypatch):
    """A transient DB error must not silently halt trading; default to True."""
    class BoomSession:
        def __enter__(self): raise RuntimeError("db down")
        def __exit__(self, *a): return False

    monkeypatch.setattr(
        "state.mysql_store.Session",
        lambda *_a, **_kw: BoomSession(),
    )
    assert store.is_strategy_enabled() is True
