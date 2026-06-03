"""Manual-close cooldown CRUD on MySQLStore."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from state.mysql_store import (
    Base,
    ManualCloseCooldownRow,
    MySQLStore,
    StrategyRow,
)


@pytest.fixture
def store():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    s = MySQLStore.__new__(MySQLStore)
    s._engine = engine
    s.strategy_name = "rsi_equity_trader"
    s._log = logging.getLogger("test_cooldowns")
    with Session(engine) as session:
        session.add(StrategyRow(name="rsi_equity_trader"))
        session.commit()
        s._strategy_id = session.query(StrategyRow).one().id
    return s


def _now() -> datetime:
    return datetime(2026, 6, 3, 12, 0, tzinfo=timezone.utc)


def test_insert_and_lookup_active_cooldown(store):
    now = _now()
    cid = store.insert_manual_close_cooldown(
        strategy_id=store._strategy_id,
        symbol="COIN",
        asset_class="equity",
        started_at=now,
        cooldown_until=now + timedelta(minutes=60),
        last_broker_qty=1.0,
        last_mysql_qty=1.0,
    )
    active = store.get_active_cooldowns(now=now + timedelta(minutes=5))
    assert len(active) == 1
    assert active[0]["id"] == cid
    assert active[0]["symbol"] == "COIN"
    assert active[0]["last_broker_qty"] == 1.0


def test_symbol_normalization_to_flat_form(store):
    now = _now()
    store.insert_manual_close_cooldown(
        strategy_id=store._strategy_id,
        symbol="BTC/USD",
        asset_class="crypto",
        started_at=now,
        cooldown_until=now + timedelta(minutes=60),
    )
    by_slash = store.get_active_cooldowns(symbol="BTC/USD", now=now)
    by_flat = store.get_active_cooldowns(symbol="BTCUSD", now=now)
    assert len(by_slash) == 1 == len(by_flat)
    assert by_slash[0]["id"] == by_flat[0]["id"]
    assert by_slash[0]["symbol"] == "BTCUSD"


def test_expired_cooldown_excluded_from_active(store):
    now = _now()
    store.insert_manual_close_cooldown(
        strategy_id=store._strategy_id,
        symbol="COIN",
        asset_class="equity",
        started_at=now,
        cooldown_until=now + timedelta(minutes=60),
    )
    after_expiry = now + timedelta(minutes=61)
    assert store.get_active_cooldowns(now=after_expiry) == []


def test_cleared_cooldown_excluded_from_active(store):
    now = _now()
    cid = store.insert_manual_close_cooldown(
        strategy_id=store._strategy_id,
        symbol="COIN",
        asset_class="equity",
        started_at=now,
        cooldown_until=now + timedelta(minutes=60),
    )
    assert store.clear_cooldown(cid, cleared_by="operator", now=now)
    assert store.get_active_cooldowns(now=now) == []
    # Idempotent — second clear is a no-op.
    assert not store.clear_cooldown(cid, cleared_by="operator", now=now)


def test_filter_by_strategy_id(store):
    with Session(store._engine) as session:
        session.add(StrategyRow(name="other_strategy"))
        session.commit()
        other_id = session.query(StrategyRow).filter(
            StrategyRow.name == "other_strategy"
        ).one().id
    now = _now()
    store.insert_manual_close_cooldown(
        strategy_id=store._strategy_id, symbol="COIN", asset_class="equity",
        started_at=now, cooldown_until=now + timedelta(minutes=60),
    )
    store.insert_manual_close_cooldown(
        strategy_id=other_id, symbol="COIN", asset_class="equity",
        started_at=now, cooldown_until=now + timedelta(minutes=60),
    )
    mine = store.get_active_cooldowns(strategy_id=store._strategy_id, now=now)
    assert len(mine) == 1
    assert mine[0]["strategy_id"] == store._strategy_id


def test_cleanup_expired_cooldowns_only_deletes_old(store):
    now = _now()
    # Long-expired
    old_cid = store.insert_manual_close_cooldown(
        strategy_id=store._strategy_id, symbol="OLD", asset_class="equity",
        started_at=now - timedelta(days=10),
        cooldown_until=now - timedelta(days=10) + timedelta(minutes=60),
    )
    # Recently expired (still in 7-day grace)
    recent_cid = store.insert_manual_close_cooldown(
        strategy_id=store._strategy_id, symbol="RECENT", asset_class="equity",
        started_at=now - timedelta(days=2),
        cooldown_until=now - timedelta(days=2) + timedelta(minutes=60),
    )
    # Active
    active_cid = store.insert_manual_close_cooldown(
        strategy_id=store._strategy_id, symbol="ACTIVE", asset_class="equity",
        started_at=now, cooldown_until=now + timedelta(minutes=60),
    )

    deleted = store.cleanup_expired_cooldowns(older_than_days=7, now=now)
    assert deleted == 1

    with Session(store._engine) as session:
        remaining_ids = {r.id for r in session.query(ManualCloseCooldownRow).all()}
    assert old_cid not in remaining_ids
    assert recent_cid in remaining_ids
    assert active_cid in remaining_ids
