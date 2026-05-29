"""Tests for state.operator_close.close_broker_only_strike.

Uses an in-memory SQLite engine + a fake AlpacaClient. Mirrors the test
fixtures in tests/test_mysql_store_operator.py.
"""
from __future__ import annotations

import glob
import json
import logging
import os
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from state.mysql_store import (
    Base,
    EventRow,
    MySQLStore,
    StrategyRow,
    StrikeRow,
)
from state.operator_close import close_broker_only_strike


class FakeAlpaca:
    """Minimal AlpacaClient stand-in. Records calls; configurable responses."""

    def __init__(self, *, positions=None, open_orders=None,
                 submit_response=None, submit_raises=None):
        self._positions = positions or []
        self._open_orders = open_orders or []
        self._submit_response = submit_response or {"id": "ord_FAKE"}
        self._submit_raises = submit_raises
        self.list_orders_calls: list[dict] = []
        self.cancel_calls: list[str] = []
        self.submit_calls: list[dict] = []

    def get_positions(self):
        return list(self._positions)

    def list_orders(self, *, status="open", symbols=None, nested=False, after=None):
        self.list_orders_calls.append({"status": status, "symbols": symbols})
        if symbols:
            return [o for o in self._open_orders if o.get("symbol") in symbols]
        return list(self._open_orders)

    def cancel_order(self, order_id):
        self.cancel_calls.append(order_id)
        return True

    def submit_order(self, *, symbol, qty, side, order_type, time_in_force,
                     client_order_id=None, limit_price=None):
        self.submit_calls.append({
            "symbol": symbol, "qty": qty, "side": side,
            "order_type": order_type, "time_in_force": time_in_force,
            "client_order_id": client_order_id,
        })
        if self._submit_raises is not None:
            raise self._submit_raises
        return dict(self._submit_response)


@pytest.fixture
def store():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    s = MySQLStore.__new__(MySQLStore)
    s._engine = engine
    s.strategy_name = "operator"
    s._log = logging.getLogger("test_operator_close")
    with Session(engine) as session:
        session.add(StrategyRow(name="operator"))
        session.commit()
    return s


@pytest.fixture(autouse=True)
def cwd_tmp(tmp_path, monkeypatch):
    """Run each test in a tmp dir so audit jsonl writes don't pollute repo."""
    monkeypatch.chdir(tmp_path)
    yield


def _make_strike(store, *, direction="broker_only", symbol="BTCUSD",
                 resolved=False, snapshot=None):
    base = datetime(2026, 5, 28, 14, 0, tzinfo=timezone.utc)
    with Session(store._engine) as session:
        row = StrikeRow(
            key=f"{direction}:{symbol}",
            direction=direction,
            strategy_id=None,
            symbol=symbol,
            strike_count=3,
            first_seen_at=base,
            last_seen_at=base + timedelta(seconds=120),
            last_observed_state=snapshot or {"mysql_sum": 0.0, "broker_qty": 0.5},
            resolved=resolved,
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        return row.id


def _audit_files():
    return sorted(glob.glob("runtime/operator_close_audit_*.jsonl"))


# ── happy path ───────────────────────────────────────────────────────


def test_happy_path_long_position(store):
    strike_id = _make_strike(store, symbol="BTCUSD")
    alpaca = FakeAlpaca(
        positions=[{"symbol": "BTCUSD", "qty": "0.5", "side": "long"}],
        open_orders=[{"id": "ord_1", "symbol": "BTCUSD"}],
        submit_response={"id": "ord_CLOSE"},
    )

    result = close_broker_only_strike(
        store=store, alpaca=alpaca, strike_id=strike_id,
        operator_note="manual leftover from yesterday",
    )

    assert result.status == "submitted"
    assert result.symbol == "BTCUSD"
    assert result.alpaca_order_id == "ord_CLOSE"
    assert result.coid is not None and "exit" in result.coid
    assert alpaca.cancel_calls == ["ord_1"]
    assert len(alpaca.submit_calls) == 1
    call = alpaca.submit_calls[0]
    assert call["symbol"] == "BTCUSD"
    assert call["qty"] == 0.5
    assert call["side"] == "sell"
    assert call["order_type"] == "market"
    assert call["time_in_force"] == "gtc"
    assert call["client_order_id"] == result.coid

    # Strike resolved with operator reason.
    with Session(store._engine) as session:
        row = session.query(StrikeRow).filter(StrikeRow.id == strike_id).one()
        assert row.resolved is True
        assert row.resolved_reason == "operator_closed_broker_only"
        events = session.query(EventRow).all()
    # operator_action event from resolve_strike contains the note.
    op_evts = [e for e in events if e.type == "operator_action"]
    assert len(op_evts) == 1
    assert "manual leftover from yesterday" in json.dumps(op_evts[0].payload)

    # Audit jsonl written.
    files = _audit_files()
    assert len(files) == 1
    rec = json.loads(open(files[0]).read().strip())
    assert rec["action"] == "operator_close_submitted"
    assert rec["client_order_id"] == result.coid
    assert rec["alpaca_order_id"] == "ord_CLOSE"


def test_short_position_closes_with_buy(store):
    strike_id = _make_strike(store, symbol="AAPL",
                             snapshot={"mysql_sum": 0.0, "broker_qty": -10.0})
    alpaca = FakeAlpaca(
        positions=[{"symbol": "AAPL", "qty": "-10", "side": "short"}],
    )
    result = close_broker_only_strike(
        store=store, alpaca=alpaca, strike_id=strike_id,
        operator_note="manual short cleanup",
    )
    assert result.status == "submitted"
    assert alpaca.submit_calls[0]["side"] == "buy"
    assert alpaca.submit_calls[0]["qty"] == 10.0


# ── already flat ─────────────────────────────────────────────────────


def test_already_flat_resolves_without_submitting(store):
    strike_id = _make_strike(store, symbol="BTCUSD")
    alpaca = FakeAlpaca(positions=[])  # broker no longer holds the symbol

    result = close_broker_only_strike(
        store=store, alpaca=alpaca, strike_id=strike_id,
        operator_note="checking — should be flat already",
    )

    assert result.status == "already_flat"
    assert alpaca.submit_calls == []
    assert alpaca.cancel_calls == []
    with Session(store._engine) as session:
        row = session.query(StrikeRow).filter(StrikeRow.id == strike_id).one()
        assert row.resolved is True
        assert row.resolved_reason == "reconciled_gone_at_close_time"


# ── direction guard ──────────────────────────────────────────────────


def test_wrong_direction_raises(store):
    strike_id = _make_strike(store, direction="mysql_only", symbol="AAPL")
    alpaca = FakeAlpaca(positions=[{"symbol": "AAPL", "qty": "1", "side": "long"}])

    with pytest.raises(ValueError, match="broker_only"):
        close_broker_only_strike(
            store=store, alpaca=alpaca, strike_id=strike_id,
            operator_note="should not fire",
        )

    assert alpaca.submit_calls == []
    with Session(store._engine) as session:
        row = session.query(StrikeRow).filter(StrikeRow.id == strike_id).one()
        assert row.resolved is False


# ── already resolved guard ───────────────────────────────────────────


def test_already_resolved_is_noop(store):
    strike_id = _make_strike(store, resolved=True)
    alpaca = FakeAlpaca(positions=[{"symbol": "BTCUSD", "qty": "0.5", "side": "long"}])

    result = close_broker_only_strike(
        store=store, alpaca=alpaca, strike_id=strike_id,
        operator_note="should noop",
    )
    assert result.status == "noop_already_resolved"
    assert alpaca.submit_calls == []


# ── submit failure leaves strike unresolved ──────────────────────────


def test_submit_failure_keeps_strike_open(store):
    strike_id = _make_strike(store, symbol="BTCUSD")
    alpaca = FakeAlpaca(
        positions=[{"symbol": "BTCUSD", "qty": "0.5", "side": "long"}],
        submit_raises=RuntimeError("alpaca said no"),
    )

    result = close_broker_only_strike(
        store=store, alpaca=alpaca, strike_id=strike_id,
        operator_note="retry case",
    )

    assert result.status == "submit_failed"
    assert "alpaca said no" in (result.error or "")
    with Session(store._engine) as session:
        row = session.query(StrikeRow).filter(StrikeRow.id == strike_id).one()
        assert row.resolved is False
    files = _audit_files()
    assert len(files) == 1
    rec = json.loads(open(files[0]).read().strip())
    assert rec["action"] == "operator_close_submit_failed"
    assert "alpaca said no" in rec["error"]


# ── operator note required ───────────────────────────────────────────


@pytest.mark.parametrize("note", ["", "  ", "ab"])
def test_empty_or_short_note_rejected(store, note):
    strike_id = _make_strike(store)
    alpaca = FakeAlpaca(positions=[{"symbol": "BTCUSD", "qty": "0.5", "side": "long"}])
    with pytest.raises(ValueError, match="operator_note"):
        close_broker_only_strike(
            store=store, alpaca=alpaca, strike_id=strike_id,
            operator_note=note,
        )
    assert alpaca.submit_calls == []
    with Session(store._engine) as session:
        row = session.query(StrikeRow).filter(StrikeRow.id == strike_id).one()
        assert row.resolved is False
