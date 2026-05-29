"""Tests for scripts/reconcile_resolve.py — the operator CLI.

Uses direct function calls (not subprocess) because the CLI subcommands are
exposed as top-level functions in reconcile_resolve. Each test gets a fresh
in-memory SQLite engine via the same fixture pattern as
tests/test_mysql_store_operator.py.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import scripts.reconcile_resolve as cli
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
    s.strategy_name = "operator"
    s._log = logging.getLogger("test_cli")
    with Session(engine) as session:
        session.add_all([
            StrategyRow(name="vwap_wave"),
            StrategyRow(name="rsi_equity"),
        ])
        session.commit()
        rows = session.query(StrategyRow).order_by(StrategyRow.id).all()
    s._strategy_id = rows[0].id
    s._other_strategy_id = rows[1].id
    return s


def _add_strike(store, *, direction="qty_drift", symbol="AAPL",
                strategy_id=None, count=3, key=None) -> int:
    base = datetime(2026, 5, 28, 14, 0, tzinfo=timezone.utc)
    with Session(store._engine) as session:
        if key is None:
            key = f"{direction}:{symbol}" if strategy_id is None \
                else f"{direction}:{strategy_id}:{symbol}"
        row = StrikeRow(
            key=key, direction=direction, strategy_id=strategy_id,
            symbol=symbol, strike_count=count,
            first_seen_at=base, last_seen_at=base,
            last_observed_state={"mysql_sum": 2.0, "broker_qty": 1.0},
            resolved=False,
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        return row.id


def _add_open_position(store, strategy_id: int, symbol: str, setup: str,
                       qty: float = 1.0,
                       coid: str = "aitrader__vwap_wave__bounce__AAPL__entry__abcd1234"):
    pos = OpenPosition(
        symbol=symbol, setup=setup, side="long", qty=qty,
        entry_px=100.0, stop_px=99.0, target_px=101.0,
        opened_at=datetime(2026, 5, 28, 13, 0, tzinfo=timezone.utc),
        order_id="o", initial_stop_px=99.0, client_order_id=coid,
    )
    saved = store._strategy_id
    store._strategy_id = strategy_id
    try:
        store.position_opened(pos, "equity")
    finally:
        store._strategy_id = saved


# ── list ─────────────────────────────────────────────────────────────


def test_cmd_list_prints_unresolved_strikes(store, capsys):
    sid = _add_strike(store)
    cli.cmd_list(store)
    out = capsys.readouterr().out
    assert str(sid) in out
    assert "AAPL" in out
    assert "qty_drift" in out


def test_cmd_list_empty(store, capsys):
    cli.cmd_list(store)
    out = capsys.readouterr().out
    assert "no unresolved" in out.lower() or "0 strike" in out.lower()


# ── show ─────────────────────────────────────────────────────────────


def test_cmd_show_prints_full_detail(store, capsys):
    sid = _add_strike(store)
    cli.cmd_show(store, strike_id=sid)
    out = capsys.readouterr().out
    assert str(sid) in out
    assert "qty_drift" in out
    assert "AAPL" in out
    assert "mysql_sum" in out or "2.0" in out


def test_cmd_show_unknown_id(store, capsys):
    with pytest.raises(SystemExit):
        cli.cmd_show(store, strike_id=99999)


# ── close (mysql_only direction) ─────────────────────────────────────


def test_cmd_close_closes_position_and_resolves_strike(store, capsys):
    _add_open_position(store, store._strategy_id, "AAPL", "vwap_bounce", qty=1.0)
    sid = _add_strike(store, direction="mysql_only", symbol="AAPL",
                      strategy_id=store._strategy_id,
                      key=f"mysql_only:{store._strategy_id}:AAPL")
    cli.cmd_close(store, strike_id=sid, exit_px=100.5,
                  reason="operator_closed_position",
                  setup="vwap_bounce", note="closed by hand on broker")
    with Session(store._engine) as session:
        pos = session.query(PositionRow).one()
        assert pos.status == "closed"
        assert pos.close_reason == "operator_closed_position"
        strike = session.query(StrikeRow).one()
        assert strike.resolved is True
        events = [e.type for e in session.query(EventRow).all()]
        assert "operator_action" in events


def test_cmd_close_rejects_non_mysql_only_strike(store, capsys):
    sid = _add_strike(store, direction="qty_drift")
    with pytest.raises(SystemExit):
        cli.cmd_close(store, strike_id=sid, exit_px=100.5,
                      reason="operator_closed_position",
                      setup="vwap_bounce", note="x")


# ── force-zero ───────────────────────────────────────────────────────


def test_cmd_force_zero_closes_position_with_zero_pnl(store, capsys):
    _add_open_position(store, store._strategy_id, "AAPL", "vwap_bounce")
    sid = _add_strike(store, direction="mysql_only", symbol="AAPL",
                      strategy_id=store._strategy_id,
                      key=f"mysql_only:{store._strategy_id}:AAPL")
    cli.cmd_force_zero(store, strike_id=sid, setup="vwap_bounce",
                       note="known phantom row")
    with Session(store._engine) as session:
        pos = session.query(PositionRow).one()
        assert pos.status == "closed"
        assert pos.close_reason == "reconciled_gone"
        assert pos.pnl_usd == 0
        strike = session.query(StrikeRow).one()
        assert strike.resolved is True


# ── adopt (broker_only direction) ────────────────────────────────────


def test_cmd_adopt_inserts_position_with_synthetic_coid(store, capsys):
    sid = _add_strike(store, direction="broker_only", symbol="SOLUSD")
    cli.cmd_adopt(store, strike_id=sid, strategy_name="vwap_wave",
                  setup="adopted", side="long", qty=10.0, entry_px=100.0,
                  asset_class="crypto", note="manually opened on broker")
    with Session(store._engine) as session:
        pos = session.query(PositionRow).one()
        assert pos.adopted is True
        assert pos.symbol == "SOLUSD"
        assert pos.client_order_id is not None
        from broker.client_order_id import parse_client_order_id
        parsed = parse_client_order_id(pos.client_order_id)
        assert parsed is not None
        assert parsed["role"] == "adopted"
        assert parsed["strategy"] == "vwap_wave"
        strike = session.query(StrikeRow).one()
        assert strike.resolved is True


def test_cmd_adopt_rejects_non_broker_only_strike(store, capsys):
    sid = _add_strike(store, direction="qty_drift")
    with pytest.raises(SystemExit):
        cli.cmd_adopt(store, strike_id=sid, strategy_name="vwap_wave",
                      setup="adopted", side="long", qty=1.0, entry_px=100.0,
                      asset_class="equity", note="x")


def test_cmd_adopt_rejects_unknown_strategy(store, capsys):
    sid = _add_strike(store, direction="broker_only", symbol="SOLUSD")
    with pytest.raises(SystemExit):
        cli.cmd_adopt(store, strike_id=sid, strategy_name="ghost_strategy",
                      setup="adopted", side="long", qty=1.0, entry_px=100.0,
                      asset_class="crypto", note="x")


# ── extend ───────────────────────────────────────────────────────────


def test_cmd_extend_resets_strike_count_and_keeps_unresolved(store, capsys):
    sid = _add_strike(store, count=3)
    cli.cmd_extend(store, strike_id=sid, note="want one more cycle")
    with Session(store._engine) as session:
        strike = session.query(StrikeRow).one()
        assert strike.resolved is False
        assert strike.strike_count == 0
        events = [e.type for e in session.query(EventRow).all()]
        assert "operator_action" in events


# ── dismiss ──────────────────────────────────────────────────────────


def test_cmd_dismiss_resolves_with_operator_dismissed(store, capsys):
    sid = _add_strike(store)
    cli.cmd_dismiss(store, strike_id=sid, note="external trade by hand")
    with Session(store._engine) as session:
        strike = session.query(StrikeRow).one()
        assert strike.resolved is True
        assert strike.resolved_reason == "operator_dismissed"
