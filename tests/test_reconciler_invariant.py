"""Tests for the cross-strategy invariant checker."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from reconciler.invariant import Anomaly, check_invariant
from state.mysql_store import Base, MySQLStore, StrategyRow
from state.position_book import OpenPosition


@pytest.fixture
def store():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    s = MySQLStore.__new__(MySQLStore)
    s._engine = engine
    s.strategy_name = "reconciler"
    s._log = logging.getLogger("test_invariant")
    with Session(engine) as session:
        session.add_all([StrategyRow(name="vwap_wave"), StrategyRow(name="rsi_equity")])
        session.commit()
        rows = session.query(StrategyRow).order_by(StrategyRow.id).all()
    s._strategy_id = rows[0].id
    s._other_strategy_id = rows[1].id
    return s


def _open_pos(store, strategy_id, symbol, setup, qty, asset_class="equity",
              side="long"):
    pos = OpenPosition(
        symbol=symbol, setup=setup, side=side, qty=qty,
        entry_px=100.0, stop_px=99.0, target_px=101.0,
        opened_at=datetime(2026, 5, 28, 14, 0, tzinfo=timezone.utc),
        order_id="o", initial_stop_px=99.0,
        client_order_id=f"aitrader__x__{setup}__{symbol.replace('/','')}__entry__abcd1234",
    )
    saved = store._strategy_id
    store._strategy_id = strategy_id
    try:
        store.position_opened(pos, asset_class)
    finally:
        store._strategy_id = saved


# ── happy paths ──────────────────────────────────────────────────────


def test_match_yields_no_anomalies(store):
    _open_pos(store, store._strategy_id, "AAPL", "vwap_bounce", 10.0)
    broker = {"AAPL": 10.0}
    with Session(store._engine) as session:
        anomalies = check_invariant(session, store, broker, qty_eps=1e-6)
    assert anomalies == []


def test_cross_strategy_match(store):
    """A=3 + B=2 in MySQL, broker=5 → invariant satisfied."""
    _open_pos(store, store._strategy_id,       "AAPL", "vwap_bounce", 3.0)
    _open_pos(store, store._other_strategy_id, "AAPL", "rsi_long",    2.0)
    broker = {"AAPL": 5.0}
    with Session(store._engine) as session:
        anomalies = check_invariant(session, store, broker, qty_eps=1e-6)
    assert anomalies == []


# ── qty_drift ────────────────────────────────────────────────────────


def test_qty_drift_when_sums_diverge(store):
    """A=1 + B=1 in MySQL, broker=1 → qty_drift on AAPL."""
    _open_pos(store, store._strategy_id,       "AAPL", "vwap_bounce", 1.0)
    _open_pos(store, store._other_strategy_id, "AAPL", "rsi_long",    1.0)
    broker = {"AAPL": 1.0}
    with Session(store._engine) as session:
        anomalies = check_invariant(session, store, broker, qty_eps=1e-6)
    assert len(anomalies) == 1
    a = anomalies[0]
    assert a.direction == "qty_drift"
    assert a.symbol == "AAPL"
    assert a.strategy_id is None
    assert a.snapshot["mysql_sum"] == 2.0
    assert a.snapshot["broker_qty"] == 1.0


def test_qty_drift_within_eps_is_silent(store):
    _open_pos(store, store._strategy_id, "AAPL", "vwap_bounce", 1.0)
    broker = {"AAPL": 1.0 + 1e-9}
    with Session(store._engine) as session:
        anomalies = check_invariant(session, store, broker, qty_eps=1e-6)
    assert anomalies == []


# ── mysql_only ───────────────────────────────────────────────────────


def test_mysql_only_when_broker_missing_symbol(store):
    """Open in MySQL, gone from broker → one mysql_only per (strategy, symbol)."""
    _open_pos(store, store._strategy_id,       "AAPL", "vwap_bounce", 1.0)
    _open_pos(store, store._other_strategy_id, "AAPL", "rsi_long",    1.0)
    broker = {}  # broker has nothing
    with Session(store._engine) as session:
        anomalies = check_invariant(session, store, broker, qty_eps=1e-6)
    directions = sorted(a.direction for a in anomalies)
    assert directions == ["mysql_only", "mysql_only"]
    strategy_ids = sorted(a.strategy_id for a in anomalies)
    assert strategy_ids == [store._strategy_id, store._other_strategy_id]


# ── broker_only ──────────────────────────────────────────────────────


def test_broker_only_when_mysql_has_no_row(store):
    broker = {"SOLUSD": 100.0}
    with Session(store._engine) as session:
        anomalies = check_invariant(session, store, broker, qty_eps=1e-6)
    assert len(anomalies) == 1
    a = anomalies[0]
    assert a.direction == "broker_only"
    assert a.symbol == "SOLUSD"
    assert a.strategy_id is None
    assert a.snapshot["broker_qty"] == 100.0


# ── crypto symbol normalization ──────────────────────────────────────


def test_crypto_slash_form_aggregates_with_flat(store):
    """Position stored as BTC/USD, broker reports BTCUSD → match (no anomaly)."""
    _open_pos(store, store._strategy_id, "BTC/USD", "vwap_bounce", 0.5,
              asset_class="crypto")
    broker = {"BTCUSD": 0.5}
    with Session(store._engine) as session:
        anomalies = check_invariant(session, store, broker, qty_eps=1e-6)
    assert anomalies == []


# ── side drift ───────────────────────────────────────────────────────


def test_qty_drift_detects_long_in_mysql_vs_short_on_broker(store):
    """MySQL records a 136-share long; broker shows a 136-share short.

    The old |qty|-based invariant treated this as no drift (|136| == |136|).
    With signed-qty comparison, this is +136 vs -136 → drift of 272.
    """
    _open_pos(store, store._strategy_id, "QQQ", "vwap_bounce", 136.0, side="long")
    broker = {"QQQ": -136.0}  # Alpaca returns signed qty; short = negative
    with Session(store._engine) as session:
        anomalies = check_invariant(session, store, broker, qty_eps=1e-6)
    assert len(anomalies) == 1
    a = anomalies[0]
    assert a.direction == "qty_drift"
    assert a.symbol == "QQQ"
    assert a.snapshot["mysql_sum"] == 136.0
    assert a.snapshot["broker_qty"] == -136.0


def test_short_match_yields_no_anomaly(store):
    """MySQL short matches broker short (both negative-qty / side=short)."""
    _open_pos(store, store._strategy_id, "QQQ", "vwap_bounce", 136.0, side="short")
    broker = {"QQQ": -136.0}
    with Session(store._engine) as session:
        anomalies = check_invariant(session, store, broker, qty_eps=1e-6)
    assert anomalies == []


def test_cross_strategy_long_short_cancellation(store):
    """A goes long 100, B goes short 100 — net MySQL = 0; broker also 0.

    Tricky edge case: the symbol appears in MySQL but with net qty of zero.
    sum_qty_by_symbol returns 0; broker doesn't list the symbol → mysql_only
    would *almost* fire. The invariant treats sum=0 specially: no anomaly
    if broker also has 0 (i.e., symbol absent from broker_norm). The current
    implementation handles this because mysql_only is `set(mysql_sums) -
    set(broker_norm)`, but a zero-net-symbol still appears in mysql_sums.

    This test documents the current behavior: a cross-cancelled symbol is
    flagged as mysql_only when broker has nothing.
    """
    _open_pos(store, store._strategy_id,       "QQQ", "vwap_bounce", 100.0, side="long")
    _open_pos(store, store._other_strategy_id, "QQQ", "rsi_long",    100.0, side="short")
    broker = {}  # broker has no QQQ
    with Session(store._engine) as session:
        anomalies = check_invariant(session, store, broker, qty_eps=1e-6)
    # Both rows produce a mysql_only anomaly (one per strategy).
    directions = sorted(a.direction for a in anomalies)
    assert directions == ["mysql_only", "mysql_only"]
