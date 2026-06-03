"""detect_manual_close — broker-side manual close detection + cooldown insert.

Covers all key scenarios from specs/manual-close-cooldown.md.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from broker.client_order_id import Role, make_client_order_id
from reconciler.config import ReconcilerConfig
from reconciler.invariant import Anomaly
from reconciler.main import detect_manual_close
from state.mysql_store import (
    Base,
    EventRow,
    ManualCloseCooldownRow,
    MySQLStore,
    PositionRow,
    StrategyRow,
    StrikeRow,
    TradeRow,
)


# ── Fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture
def store():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    s = MySQLStore.__new__(MySQLStore)
    s._engine = engine
    s.strategy_name = "vwap_bands_crypto_trader"
    s._log = logging.getLogger("test_manual_close")
    with Session(engine) as session:
        session.add(StrategyRow(name="vwap_bands_crypto_trader"))
        session.commit()
        s._strategy_id = session.query(StrategyRow).one().id
    return s


def _cfg(*, shadow=False, confirm_cycles=2, cooldown_min=60, min_gap_s=0):
    """Default min_gap_s=0 so consecutive cycles in tests don't get rate-limited."""
    return ReconcilerConfig(
        interval_s=30,
        strike_threshold=3,
        strike_min_gap_s=min_gap_s,
        qty_eps=1e-6,
        shadow_mode=shadow,
        state_file_path="/tmp/state.json",
        heartbeat_stale_after_s=300,
        auto_close_max_notional_usd=190_000.0,
        auto_close_dust_usd=1.0,
        asset_class="crypto",
        manual_close_confirm_cycles=confirm_cycles,
        manual_close_cooldown_min=cooldown_min,
    )


def _entry_coid(strategy="vwap_bands_crypto_trader", setup="vwap_bands",
                symbol="COIN"):
    return make_client_order_id(strategy, setup, symbol, Role.ENTRY)


def _seed_open_position(
    session, store, *, symbol="COIN", setup="vwap_bands", qty=1.0,
    entry_px=250.0, asset_class="crypto", strategy="vwap_bands_crypto_trader",
    strategy_id=None,
) -> PositionRow:
    coid = _entry_coid(strategy=strategy, setup=setup, symbol=symbol)
    sid = strategy_id if strategy_id is not None else store._strategy_id
    row = PositionRow(
        strategy_id=sid, setup_name=setup, symbol=symbol,
        side="long", qty=qty, entry_px=entry_px,
        opened_at=datetime(2026, 6, 3, 12, 0, tzinfo=timezone.utc),
        asset_class=asset_class, status="open",
        client_order_id=coid, legacy_untagged=0,
    )
    session.add(row)
    session.flush()
    return row


def _anomaly(symbol="COIN", strategy_id=1, mysql_qty=1.0):
    return Anomaly(
        direction="mysql_only",
        symbol=symbol,
        strategy_id=strategy_id,
        snapshot={"mysql_qty": mysql_qty, "broker_qty": 0.0},
        asset_class="crypto",
    )


def _filled_order(coid, symbol="COIN", filled_qty="1"):
    return {
        "client_order_id": coid,
        "symbol": symbol,
        "status": "filled",
        "filled_qty": filled_qty,
    }


# ── US-1: detect manual close, create cooldown ──────────────────────────────

def test_detects_manual_close_after_confirm_cycles(store):
    """Two consecutive cycles confirm → MySQL row closed, cooldown inserted."""
    cfg = _cfg(confirm_cycles=2)
    now = datetime(2026, 6, 3, 12, 0, tzinfo=timezone.utc)

    with Session(store._engine) as session:
        row = _seed_open_position(session, store)
        coid = row.client_order_id
        session.commit()

    alpaca = MagicMock()
    alpaca.list_orders.return_value = [_filled_order(coid)]

    a = _anomaly(strategy_id=store._strategy_id)

    # Cycle 1 — candidate, strike at 1, no close.
    with Session(store._engine) as session:
        n = detect_manual_close(
            alpaca=alpaca, store=store, session=session,
            anomalies=[a], recent_fills=[], cfg=cfg, now=now,
            asset_class="crypto",
        )
        session.commit()
    assert n == 0
    with Session(store._engine) as session:
        strikes = session.query(StrikeRow).filter(
            StrikeRow.direction == "manual_close",
        ).all()
        assert len(strikes) == 1
        assert strikes[0].strike_count == 1
        assert strikes[0].resolved is False

    # Cycle 2 — confirmation reached, close + cooldown.
    with Session(store._engine) as session:
        n = detect_manual_close(
            alpaca=alpaca, store=store, session=session,
            anomalies=[a], recent_fills=[], cfg=cfg, now=now + timedelta(seconds=70),
            asset_class="crypto",
        )
        session.commit()
    assert n == 1

    with Session(store._engine) as session:
        # Position closed with reason=manual_close
        position = session.query(PositionRow).one()
        assert position.status == "closed"
        assert position.close_reason == "manual_close"
        # Cooldown row inserted
        cooldown = session.query(ManualCloseCooldownRow).one()
        assert cooldown.strategy_id == store._strategy_id
        assert cooldown.symbol == "COIN"
        assert cooldown.cleared_at is None
        assert (cooldown.cooldown_until - cooldown.started_at) == timedelta(minutes=60)
        # Strike resolved
        strike = session.query(StrikeRow).filter(
            StrikeRow.direction == "manual_close",
        ).one()
        assert strike.resolved is True
        assert strike.resolved_reason == "manual_close_confirmed"
        # manual_close event emitted
        events = session.query(EventRow).filter(
            EventRow.type == "manual_close",
        ).all()
        assert len(events) == 1
        assert events[0].symbol == "COIN"


# ── Engine-issued exit must NOT trigger cooldown ────────────────────────────

def test_aitrader_exit_fill_in_recent_fills_suppresses_detection(store):
    """If an exit/stop/target COID matches the row, NOT a manual close."""
    cfg = _cfg(confirm_cycles=2)
    now = datetime(2026, 6, 3, 12, 0, tzinfo=timezone.utc)

    with Session(store._engine) as session:
        row = _seed_open_position(session, store)
        entry_coid = row.client_order_id
        session.commit()

    alpaca = MagicMock()
    alpaca.list_orders.return_value = [_filled_order(entry_coid)]

    exit_coid = make_client_order_id(
        "vwap_bands_crypto_trader", "vwap_bands", "COIN", Role.EXIT,
    )
    recent_fills = [{"client_order_id": exit_coid, "symbol": "COIN"}]

    a = _anomaly(strategy_id=store._strategy_id)

    # Even after many cycles, no detection.
    with Session(store._engine) as session:
        for i in range(5):
            detect_manual_close(
                alpaca=alpaca, store=store, session=session,
                anomalies=[a], recent_fills=recent_fills,
                cfg=cfg, now=now + timedelta(seconds=70 * i),
                asset_class="crypto",
            )
            session.commit()

    with Session(store._engine) as session:
        assert session.query(ManualCloseCooldownRow).count() == 0
        assert session.query(PositionRow).filter(
            PositionRow.status == "closed",
        ).count() == 0
        # No manual_close strike row created either.
        assert session.query(StrikeRow).filter(
            StrikeRow.direction == "manual_close",
        ).count() == 0


def test_stop_role_fill_suppresses_detection(store):
    """Stop-loss bracket child filling is also an aitrader-issued exit."""
    cfg = _cfg(confirm_cycles=2)
    now = datetime(2026, 6, 3, 12, 0, tzinfo=timezone.utc)

    with Session(store._engine) as session:
        row = _seed_open_position(session, store)
        entry_coid = row.client_order_id
        session.commit()

    alpaca = MagicMock()
    alpaca.list_orders.return_value = [_filled_order(entry_coid)]
    stop_coid = make_client_order_id(
        "vwap_bands_crypto_trader", "vwap_bands", "COIN", Role.STOP,
    )
    recent_fills = [{"client_order_id": stop_coid, "symbol": "COIN"}]
    a = _anomaly(strategy_id=store._strategy_id)

    with Session(store._engine) as session:
        for i in range(3):
            detect_manual_close(
                alpaca=alpaca, store=store, session=session,
                anomalies=[a], recent_fills=recent_fills,
                cfg=cfg, now=now + timedelta(seconds=70 * i),
                asset_class="crypto",
            )
            session.commit()
        assert session.query(ManualCloseCooldownRow).count() == 0


# ── Entry never filled is NOT a manual close ────────────────────────────────

def test_entry_never_filled_is_not_a_manual_close(store):
    """If the entry COID never shows as filled at the broker, this is the
    entry_never_filled case (handled by a sibling pass), not a manual close.
    """
    cfg = _cfg(confirm_cycles=2)
    now = datetime(2026, 6, 3, 12, 0, tzinfo=timezone.utc)

    with Session(store._engine) as session:
        _seed_open_position(session, store)
        session.commit()

    alpaca = MagicMock()
    # Closed orders return empty — entry never filled.
    alpaca.list_orders.return_value = []
    a = _anomaly(strategy_id=store._strategy_id)

    with Session(store._engine) as session:
        for i in range(3):
            detect_manual_close(
                alpaca=alpaca, store=store, session=session,
                anomalies=[a], recent_fills=[],
                cfg=cfg, now=now + timedelta(seconds=70 * i),
                asset_class="crypto",
            )
            session.commit()
        assert session.query(ManualCloseCooldownRow).count() == 0
        assert session.query(StrikeRow).filter(
            StrikeRow.direction == "manual_close",
        ).count() == 0


# ── Idempotency — re-detect with active cooldown is no-op ───────────────────

def test_redetect_with_active_cooldown_is_noop(store):
    """A second confirmation while a cooldown is already active emits
    manual_close_redetected, doesn't insert a new row.
    """
    cfg = _cfg(confirm_cycles=1)  # confirm immediately for setup speed
    now = datetime(2026, 6, 3, 12, 0, tzinfo=timezone.utc)

    with Session(store._engine) as session:
        row = _seed_open_position(session, store)
        coid = row.client_order_id
        session.commit()

    alpaca = MagicMock()
    alpaca.list_orders.return_value = [_filled_order(coid)]
    a = _anomaly(strategy_id=store._strategy_id)

    # Confirm cycle 1 — close + cooldown.
    with Session(store._engine) as session:
        detect_manual_close(
            alpaca=alpaca, store=store, session=session,
            anomalies=[a], recent_fills=[], cfg=cfg, now=now,
            asset_class="crypto",
        )
        session.commit()

    # Re-seed a synthetic open row to simulate a hypothetical re-entry that
    # somehow slipped past the filter; second confirmation should NOT close it.
    with Session(store._engine) as session:
        # Reuse the already-closed row's entry COID — irrelevant for the
        # idempotency check, which keys on the existing cooldown row.
        new_row = PositionRow(
            strategy_id=store._strategy_id, setup_name="vwap_bands",
            symbol="COIN", side="long", qty=1.0, entry_px=251.0,
            opened_at=now + timedelta(minutes=5),
            asset_class="crypto", status="open",
            client_order_id=coid, legacy_untagged=0,
        )
        session.add(new_row)
        session.commit()

    with Session(store._engine) as session:
        detect_manual_close(
            alpaca=alpaca, store=store, session=session,
            anomalies=[a], recent_fills=[],
            cfg=cfg, now=now + timedelta(minutes=5),
            asset_class="crypto",
        )
        session.commit()

    with Session(store._engine) as session:
        assert session.query(ManualCloseCooldownRow).count() == 1
        evs = session.query(EventRow).filter(
            EventRow.type == "manual_close_redetected",
        ).count()
        assert evs == 1


# ── Cross-strategy with one open row ─────────────────────────────────────────

def test_cooldown_only_for_strategy_owning_the_row(store):
    """Two strategies share BTC; only one holds an open MySQL row. Cooldown
    is created only for that strategy.
    """
    cfg = _cfg(confirm_cycles=1)
    now = datetime(2026, 6, 3, 12, 0, tzinfo=timezone.utc)

    with Session(store._engine) as session:
        # Add a second strategy
        session.add(StrategyRow(name="ib_crypto_trader"))
        session.commit()
        ib_id = session.query(StrategyRow).filter(
            StrategyRow.name == "ib_crypto_trader",
        ).one().id

        # Only ib_crypto holds the position.
        row = _seed_open_position(
            session, store, symbol="BTC", setup="ib_crypto",
            strategy="ib_crypto_trader", strategy_id=ib_id,
        )
        ib_coid = row.client_order_id
        session.commit()

    alpaca = MagicMock()
    alpaca.list_orders.return_value = [_filled_order(ib_coid, symbol="BTC")]

    a = Anomaly(
        direction="mysql_only", symbol="BTC", strategy_id=ib_id,
        snapshot={"mysql_qty": 1.0, "broker_qty": 0.0}, asset_class="crypto",
    )

    with Session(store._engine) as session:
        detect_manual_close(
            alpaca=alpaca, store=store, session=session,
            anomalies=[a], recent_fills=[], cfg=cfg, now=now,
            asset_class="crypto",
        )
        session.commit()

    with Session(store._engine) as session:
        cooldowns = session.query(ManualCloseCooldownRow).all()
        assert len(cooldowns) == 1
        assert cooldowns[0].strategy_id == ib_id
        # Different strategy got nothing.
        assert cooldowns[0].strategy_id != store._strategy_id


# ── Audit-only mode (cooldown_min=0) ─────────────────────────────────────────

def test_cooldown_zero_minutes_creates_immediately_expired_row(store):
    """MANUAL_CLOSE_COOLDOWN_MIN=0 → row inserted with cooldown_until == started_at."""
    cfg = _cfg(confirm_cycles=1, cooldown_min=0)
    now = datetime(2026, 6, 3, 12, 0, tzinfo=timezone.utc)

    with Session(store._engine) as session:
        row = _seed_open_position(session, store)
        coid = row.client_order_id
        session.commit()

    alpaca = MagicMock()
    alpaca.list_orders.return_value = [_filled_order(coid)]
    a = _anomaly(strategy_id=store._strategy_id)

    with Session(store._engine) as session:
        n = detect_manual_close(
            alpaca=alpaca, store=store, session=session,
            anomalies=[a], recent_fills=[], cfg=cfg, now=now,
            asset_class="crypto",
        )
        session.commit()
    assert n == 1
    with Session(store._engine) as session:
        cooldown = session.query(ManualCloseCooldownRow).one()
        assert cooldown.cooldown_until == cooldown.started_at


# ── Shadow mode emits events but no side effects ─────────────────────────────

def test_shadow_mode_emits_only_shadow_event(store):
    cfg = _cfg(confirm_cycles=1, shadow=True)
    now = datetime(2026, 6, 3, 12, 0, tzinfo=timezone.utc)

    with Session(store._engine) as session:
        row = _seed_open_position(session, store)
        coid = row.client_order_id
        session.commit()

    alpaca = MagicMock()
    alpaca.list_orders.return_value = [_filled_order(coid)]
    a = _anomaly(strategy_id=store._strategy_id)

    with Session(store._engine) as session:
        n = detect_manual_close(
            alpaca=alpaca, store=store, session=session,
            anomalies=[a], recent_fills=[], cfg=cfg, now=now,
            asset_class="crypto",
        )
        session.commit()
    assert n == 0
    with Session(store._engine) as session:
        assert session.query(ManualCloseCooldownRow).count() == 0
        assert session.query(PositionRow).filter(
            PositionRow.status == "closed",
        ).count() == 0
        evs = session.query(EventRow).filter(
            EventRow.type == "manual_close_shadow",
        ).all()
        assert len(evs) == 1


# ── Anomalies of other directions are ignored ────────────────────────────────

def test_only_processes_mysql_only_anomalies(store):
    cfg = _cfg(confirm_cycles=1)
    now = datetime(2026, 6, 3, 12, 0, tzinfo=timezone.utc)
    alpaca = MagicMock()
    alpaca.list_orders.return_value = []

    qty_drift = Anomaly(
        direction="qty_drift", symbol="COIN", strategy_id=None,
        snapshot={"mysql_sum": 1.0, "broker_qty": 22.0}, asset_class="crypto",
    )
    broker_only = Anomaly(
        direction="broker_only", symbol="ETH", strategy_id=None,
        snapshot={"mysql_sum": 0.0, "broker_qty": 5.0}, asset_class="crypto",
    )

    with Session(store._engine) as session:
        n = detect_manual_close(
            alpaca=alpaca, store=store, session=session,
            anomalies=[qty_drift, broker_only], recent_fills=[],
            cfg=cfg, now=now, asset_class="crypto",
        )
        session.commit()
    assert n == 0
    with Session(store._engine) as session:
        assert session.query(StrikeRow).filter(
            StrikeRow.direction == "manual_close",
        ).count() == 0
