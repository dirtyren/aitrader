"""Per-asset-class scoping for the reconciler.

After splitting the reconciler into one container per asset class, each
instance owns only its own slice of MySQL state. The pieces under test:

  - ReconcilerConfig.from_env requires RECONCILER_ASSET_CLASS.
  - StrikeRow / EventRow rows written during a cycle carry the cycle's
    asset_class.
  - auto_clear_resolved leaves the OTHER side's strikes alone (so two
    parallel reconcilers can't stomp each other's state).
  - check_invariant aggregates only its own asset_class's positions.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from reconciler.config import ReconcilerConfig
from reconciler.invariant import Anomaly, check_invariant
from reconciler.main import run_one_cycle
from reconciler.strikes import auto_clear_resolved, process_anomaly
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
    s.strategy_name = "reconciler-equity"
    s._log = logging.getLogger("test_per_asset_class")
    with Session(engine) as session:
        session.add(StrategyRow(name="vwap_wave"))
        session.commit()
        s._strategy_id = session.query(StrategyRow).one().id
    return s


def _cfg(asset_class: str | None = "equity"):
    return ReconcilerConfig(
        interval_s=30, strike_threshold=3, strike_min_gap_s=60,
        qty_eps=1e-6, shadow_mode=False,
        state_file_path="/tmp/state.json",
        heartbeat_stale_after_s=300,
        auto_close_max_notional_usd=190_000.0,
        auto_close_dust_usd=1.0,
        asset_class=asset_class,
    )


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def test_from_env_requires_asset_class(monkeypatch):
    monkeypatch.delenv("RECONCILER_ASSET_CLASS", raising=False)
    with pytest.raises(RuntimeError, match="RECONCILER_ASSET_CLASS"):
        ReconcilerConfig.from_env()


def test_from_env_rejects_unknown_asset_class(monkeypatch):
    monkeypatch.setenv("RECONCILER_ASSET_CLASS", "forex")
    with pytest.raises(RuntimeError, match="RECONCILER_ASSET_CLASS"):
        ReconcilerConfig.from_env()


def test_from_env_accepts_equity_and_crypto(monkeypatch):
    for ac in ("equity", "crypto"):
        monkeypatch.setenv("RECONCILER_ASSET_CLASS", ac)
        cfg = ReconcilerConfig.from_env()
        assert cfg.asset_class == ac


# ---------------------------------------------------------------------------
# Cycle-level scoping
# ---------------------------------------------------------------------------


def test_cycle_stamps_asset_class_on_heartbeat(store):
    alpaca = MagicMock()
    alpaca.get_positions.return_value = []
    alpaca.list_orders.return_value = []
    cfg = _cfg(asset_class="equity")
    now = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)

    run_one_cycle(
        store=store, alpaca=alpaca, cfg=cfg,
        last_orders_check_ts=None, now=now,
    )

    with Session(store._engine) as session:
        rows = session.query(EventRow).filter(
            EventRow.type == "heartbeat",
        ).all()
        assert len(rows) == 1
        assert rows[0].asset_class == "equity"


def test_cycle_stamps_asset_class_on_strike(store):
    """A broker_only anomaly observed by an equity reconciler ends up as a
    StrikeRow with asset_class='equity'."""
    alpaca = MagicMock()
    alpaca.get_positions.return_value = [{
        "symbol": "AAPL", "qty": "1", "side": "long",
        "asset_class": "us_equity",
    }]
    alpaca.list_orders.return_value = []
    cfg = _cfg(asset_class="equity")
    now = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)

    run_one_cycle(
        store=store, alpaca=alpaca, cfg=cfg,
        last_orders_check_ts=None, now=now,
    )

    with Session(store._engine) as session:
        strikes = session.query(StrikeRow).all()
        assert len(strikes) == 1
        assert strikes[0].direction == "broker_only"
        assert strikes[0].asset_class == "equity"


# ---------------------------------------------------------------------------
# Cross-side isolation
# ---------------------------------------------------------------------------


def test_auto_clear_leaves_other_asset_class_alone(store):
    """The cornerstone of safe parallel reconcilers: an equity cycle whose
    anomaly set is empty must NOT clear a crypto strike."""
    now = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    with Session(store._engine) as session:
        # Pre-existing strike from the crypto reconciler.
        crypto_strike = StrikeRow(
            key="broker_only:BTCUSD",
            direction="broker_only",
            strategy_id=None,
            symbol="BTCUSD",
            asset_class="crypto",
            strike_count=2,
            first_seen_at=now - timedelta(minutes=10),
            last_seen_at=now - timedelta(minutes=2),
            last_observed_state={"mysql_sum": 0.0, "broker_qty": 1.0},
            resolved=False,
        )
        # And an equity strike that this cycle WILL still see (so it stays).
        equity_strike = StrikeRow(
            key="broker_only:AAPL",
            direction="broker_only",
            strategy_id=None,
            symbol="AAPL",
            asset_class="equity",
            strike_count=1,
            first_seen_at=now - timedelta(minutes=5),
            last_seen_at=now - timedelta(minutes=1),
            last_observed_state={"mysql_sum": 0.0, "broker_qty": 1.0},
            resolved=False,
        )
        session.add_all([crypto_strike, equity_strike])
        session.commit()

        cleared = auto_clear_resolved(
            session,
            current_anomaly_keys={"broker_only:AAPL"},  # AAPL still seen
            now=now,
            asset_class="equity",
        )
        session.commit()

        # Only no-equity strikes that vanished get cleared. Here AAPL is
        # still present, BTCUSD is owned by crypto → nothing cleared.
        assert cleared == []

        all_strikes = session.query(StrikeRow).all()
        by_key = {s.key: s for s in all_strikes}
        assert by_key["broker_only:BTCUSD"].resolved is False
        assert by_key["broker_only:AAPL"].resolved is False


def test_auto_clear_adopts_legacy_null_asset_class(store):
    """Strikes that pre-date the migration carry NULL asset_class. The first
    cycle that runs without seeing them should clear them on its own side
    so they don't linger forever."""
    now = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    with Session(store._engine) as session:
        legacy = StrikeRow(
            key="broker_only:OLDSYM",
            direction="broker_only",
            strategy_id=None,
            symbol="OLDSYM",
            asset_class=None,
            strike_count=1,
            first_seen_at=now - timedelta(hours=1),
            last_seen_at=now - timedelta(minutes=30),
            last_observed_state={"mysql_sum": 0.0, "broker_qty": 1.0},
            resolved=False,
        )
        session.add(legacy)
        session.commit()

        cleared = auto_clear_resolved(
            session, current_anomaly_keys=set(),
            now=now, asset_class="equity",
        )
        session.commit()

        assert cleared == ["broker_only:OLDSYM"]
        row = session.query(StrikeRow).one()
        assert row.resolved is True
        assert row.resolved_reason == "self_healed"


def test_check_invariant_scopes_to_asset_class(store):
    """Two open positions in MySQL — one equity, one crypto — and a broker
    snapshot containing only AAPL. Equity-scoped invariant must NOT report
    BTCUSD as mysql_only (that's crypto's lane)."""
    now = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    with Session(store._engine) as session:
        eq_strategy = session.query(StrategyRow).one()
        session.add_all([
            PositionRow(
                strategy_id=eq_strategy.id, setup_name="vwap_bounce",
                symbol="AAPL", side="long", qty=1.0, entry_px=100.0,
                opened_at=now, asset_class="equity", status="open",
                client_order_id="coid-aapl", legacy_untagged=0,
            ),
            PositionRow(
                strategy_id=eq_strategy.id, setup_name="vwap_bounce",
                symbol="BTC/USD", side="long", qty=0.5, entry_px=50000.0,
                opened_at=now, asset_class="crypto", status="open",
                client_order_id="coid-btc", legacy_untagged=0,
            ),
        ])
        session.commit()

        broker_qty = {"AAPL": 1.0}
        anomalies = check_invariant(
            session, store, broker_qty,
            qty_eps=1e-6, asset_class="equity",
        )

        # Equity sums match (mysql=1, broker=1). Crypto's BTC must NOT
        # appear as a mysql_only — it isn't this reconciler's concern.
        assert all(a.symbol != "BTCUSD" for a in anomalies)
        assert all(a.asset_class == "equity" for a in anomalies)
