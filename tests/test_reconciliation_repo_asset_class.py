"""ui/data/reconciliation_repo asset_class filtering.

Each Reconciliation subtab (Equity / Crypto) should only see its own side's
rows. Verifies the new asset_class kwarg on every public query.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from state.mysql_store import Base, EventRow, StrategyRow, StrikeRow
from ui.data import reconciliation_repo as repo


@pytest.fixture
def engine(monkeypatch):
    eng = create_engine("sqlite://")
    Base.metadata.create_all(eng)
    with Session(eng) as session:
        session.add_all([
            StrategyRow(name="vwap_wave"),
            StrategyRow(name="ib_crypto"),
        ])
        session.commit()
    from ui.data import db
    monkeypatch.setattr(db, "_engine", eng)
    monkeypatch.setattr(db, "get_engine", lambda: eng)
    return eng


def _add_strike(engine, *, key, symbol, asset_class, last_seen_offset_s=0):
    base = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    with Session(engine) as session:
        session.add(StrikeRow(
            key=key, direction="broker_only", symbol=symbol,
            asset_class=asset_class, strike_count=1,
            first_seen_at=base,
            last_seen_at=base + timedelta(seconds=last_seen_offset_s),
            last_observed_state={"mysql_sum": 0.0, "broker_qty": 1.0},
            resolved=False,
        ))
        session.commit()


def _add_event(engine, *, type_, asset_class, offset_s=0):
    base = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    with Session(engine) as session:
        session.add(EventRow(
            type=type_, asset_class=asset_class, payload={},
            created_at=base + timedelta(seconds=offset_s),
        ))
        session.commit()


# ── strikes ──────────────────────────────────────────────────────────


def test_get_unresolved_strikes_filters_by_asset_class(engine):
    _add_strike(engine, key="broker_only:AAPL",
                symbol="AAPL", asset_class="equity")
    _add_strike(engine, key="broker_only:BTCUSD",
                symbol="BTCUSD", asset_class="crypto")
    eq = repo.get_unresolved_strikes(asset_class="equity")
    cr = repo.get_unresolved_strikes(asset_class="crypto")
    assert eq["symbol"].tolist() == ["AAPL"]
    assert cr["symbol"].tolist() == ["BTCUSD"]


def test_get_unresolved_strikes_unfiltered_returns_all(engine):
    _add_strike(engine, key="broker_only:AAPL",
                symbol="AAPL", asset_class="equity")
    _add_strike(engine, key="broker_only:BTCUSD",
                symbol="BTCUSD", asset_class="crypto")
    df = repo.get_unresolved_strikes()
    assert sorted(df["symbol"].tolist()) == ["AAPL", "BTCUSD"]


def test_get_unresolved_strikes_excludes_other_class_nulls(engine):
    """A NULL asset_class strike (legacy) does NOT show up under either
    side until a reconciler adopts it. This keeps the dashboard honest:
    operator can't act on something not assigned a lane yet."""
    _add_strike(engine, key="broker_only:OLD",
                symbol="OLD", asset_class=None)
    eq = repo.get_unresolved_strikes(asset_class="equity")
    cr = repo.get_unresolved_strikes(asset_class="crypto")
    assert eq.empty
    assert cr.empty


# ── events ───────────────────────────────────────────────────────────


def test_get_recent_events_filters_by_asset_class(engine):
    _add_event(engine, type_="heartbeat", asset_class="equity", offset_s=1)
    _add_event(engine, type_="heartbeat", asset_class="crypto", offset_s=2)
    _add_event(engine, type_="auto_close_dust", asset_class="equity",
               offset_s=3)

    eq = repo.get_recent_events(asset_class="equity")
    cr = repo.get_recent_events(asset_class="crypto")
    assert sorted(eq["type"].tolist()) == ["auto_close_dust", "heartbeat"]
    assert cr["type"].tolist() == ["heartbeat"]


# ── heartbeat ────────────────────────────────────────────────────────


def test_get_heartbeat_freshness_per_asset_class(engine):
    _add_event(engine, type_="heartbeat", asset_class="equity", offset_s=10)
    _add_event(engine, type_="heartbeat", asset_class="crypto", offset_s=100)

    eq = repo.get_heartbeat_freshness(asset_class="equity")
    cr = repo.get_heartbeat_freshness(asset_class="crypto")

    # The crypto one is more recent (offset=100 > 10), so its age must be
    # smaller than equity's. Both are non-None.
    assert eq["last_seen_at"] is not None
    assert cr["last_seen_at"] is not None
    assert cr["age_seconds"] < eq["age_seconds"]


def test_get_heartbeat_freshness_returns_none_when_side_silent(engine):
    _add_event(engine, type_="heartbeat", asset_class="equity", offset_s=10)
    cr = repo.get_heartbeat_freshness(asset_class="crypto")
    assert cr["last_seen_at"] is None
    assert cr["age_seconds"] is None
