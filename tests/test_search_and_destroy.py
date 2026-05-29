"""Tests for scripts/search_and_destroy.py — destructive clean-slate tool."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import scripts.search_and_destroy as sd
from broker.client_order_id import parse_client_order_id
from state.mysql_store import (
    Base,
    EventRow,
    MySQLStore,
    PositionRow,
    StrategyRow,
    StrikeRow,
    TradeRow,
)


_ACCOUNT_NUMBER = "PA3A775CQ0RN"


@pytest.fixture
def store():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    s = MySQLStore.__new__(MySQLStore)
    s._engine = engine
    s.strategy_name = "operator"
    s._log = logging.getLogger("test_sad")
    with Session(engine) as session:
        session.add_all([
            StrategyRow(name="vwap_wave"),
            StrategyRow(name="rsi_crypto"),
        ])
        session.commit()
        rows = session.query(StrategyRow).order_by(StrategyRow.id).all()
    s._strategy_id = rows[0].id
    s._other_strategy_id = rows[1].id
    return s


@pytest.fixture
def alpaca():
    a = MagicMock()
    a.get_account.return_value = {
        "id": "abc-123", "account_number": _ACCOUNT_NUMBER,
    }
    a.get_positions.return_value = [
        {"symbol": "AAPL", "qty": "10", "side": "long", "asset_class": "us_equity"},
        {"symbol": "QQQ", "qty": "-5", "side": "short", "asset_class": "us_equity"},
    ]
    a.list_orders.return_value = [
        {"id": "ord-1", "symbol": "AAPL"},
        {"id": "ord-2", "symbol": "QQQ"},
    ]
    a.cancel_order.return_value = True
    a.submit_order.return_value = {"id": "close-1"}
    return a


def _populate_state(store):
    """Insert representative rows into every wipe-target table."""
    base = datetime(2026, 5, 28, 14, 0, tzinfo=timezone.utc)
    with Session(store._engine) as session:
        session.add(PositionRow(
            strategy_id=store._strategy_id, symbol="AAPL",
            asset_class="us_equity", side="long",
            qty=Decimal("10"), entry_px=Decimal("100"),
            setup_name="vwap_bounce", order_id="", status="open",
            opened_at=base,
        ))
        session.add(TradeRow(
            strategy_id=store._strategy_id, symbol="MSFT",
            asset_class="us_equity", setup_name="vwap_bounce", side="long",
            qty=Decimal("5"), entry_px=Decimal("100"), exit_px=Decimal("105"),
            pnl_usd=Decimal("25"), R_realized=Decimal("1"),
            close_reason="target", opened_at=base, closed_at=base,
        ))
        session.add(StrikeRow(
            key="qty_drift:AAPL", direction="qty_drift", symbol="AAPL",
            strike_count=1, first_seen_at=base, last_seen_at=base,
            last_observed_state={}, resolved=False,
        ))
        session.add(EventRow(type="heartbeat", created_at=base))
        session.commit()


# ── argparse / confirmation ──────────────────────────────────────────


def test_dry_run_and_apply_are_mutually_exclusive():
    with pytest.raises(SystemExit):
        sd.main(["--dry-run", "--apply"])
    with pytest.raises(SystemExit):
        sd.main([])


def test_apply_without_confirm_account_fails(monkeypatch, store, alpaca, capsys):
    monkeypatch.setattr(sd, "AlpacaClient", lambda *a, **k: alpaca)
    monkeypatch.setattr(sd, "MySQLStore", lambda *a, **k: store)
    rc = sd.main(["--apply"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "--confirm-account" in err


def test_apply_with_wrong_account_number_refuses(monkeypatch, store, alpaca, capsys):
    monkeypatch.setattr(sd, "AlpacaClient", lambda *a, **k: alpaca)
    monkeypatch.setattr(sd, "MySQLStore", lambda *a, **k: store)
    rc = sd.main(["--apply", "--confirm-account", "WRONG-ACCOUNT"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "does not match" in err


# ── dry-run ──────────────────────────────────────────────────────────


def test_dry_run_lists_state_and_writes_nothing(monkeypatch, store, alpaca,
                                                 capsys, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sd, "AlpacaClient", lambda *a, **k: alpaca)
    monkeypatch.setattr(sd, "MySQLStore", lambda *a, **k: store)
    _populate_state(store)

    rc = sd.main(["--dry-run"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "AAPL" in out and "QQQ" in out
    assert "dry-run" in out.lower()

    # Alpaca close paths NOT taken
    alpaca.submit_order.assert_not_called()
    alpaca.cancel_order.assert_not_called()
    # MySQL not truncated
    with Session(store._engine) as session:
        assert session.query(PositionRow).count() == 1
        assert session.query(TradeRow).count() == 1
        assert session.query(StrikeRow).count() == 1
    # No audit file
    assert list(tmp_path.glob("runtime/clean_slate_audit_*.jsonl")) == []


# ── apply happy path ─────────────────────────────────────────────────


def test_apply_full_flow_cancels_orders_closes_positions_and_truncates(
    monkeypatch, store, alpaca, capsys, tmp_path,
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sd, "AlpacaClient", lambda *a, **k: alpaca)
    monkeypatch.setattr(sd, "MySQLStore", lambda *a, **k: store)
    _populate_state(store)

    # After close, broker is flat
    after_close_state = {"called": False}
    def _get_positions_after_close():
        if not after_close_state["called"]:
            after_close_state["called"] = True
            return [{"symbol": "AAPL", "qty": "10", "side": "long",
                     "asset_class": "us_equity"},
                    {"symbol": "QQQ", "qty": "-5", "side": "short",
                     "asset_class": "us_equity"}]
        return []
    alpaca.get_positions = _get_positions_after_close

    rc = sd.main([
        "--apply", "--confirm-account", _ACCOUNT_NUMBER,
        "--poll-seconds", "1",
    ])
    assert rc == 0

    # Cancel + submit close were called
    assert alpaca.cancel_order.call_count == 2
    assert alpaca.submit_order.call_count == 2

    # COIDs on close orders are role=exit, parseable
    for call in alpaca.submit_order.call_args_list:
        coid = call.kwargs["client_order_id"]
        parsed = parse_client_order_id(coid)
        assert parsed is not None
        assert parsed["role"] == "exit"
        assert parsed["strategy"] == "operator"
        assert parsed["setup"] == "cleanslate"

    # AAPL: long → sell-to-close. QQQ: short → buy-to-cover.
    submit_calls = {c.kwargs["symbol"]: c for c in alpaca.submit_order.call_args_list}
    assert submit_calls["AAPL"].kwargs["side"] == "sell"
    assert submit_calls["AAPL"].kwargs["qty"] == 10.0
    assert submit_calls["QQQ"].kwargs["side"] == "buy"
    # Qty is positive on the close order, regardless of broker's signed qty
    assert submit_calls["QQQ"].kwargs["qty"] == 5.0

    # MySQL truncated for the wipe targets, strategies preserved.
    # Note: the script itself calls upsert_strategy() with name='operator'
    # to satisfy MySQLStore's constructor invariant — that's a third
    # StrategyRow alongside the fixture's 'vwap_wave' and 'rsi_crypto'.
    with Session(store._engine) as session:
        assert session.query(PositionRow).count() == 0
        assert session.query(TradeRow).count() == 0
        assert session.query(StrikeRow).count() == 0
        assert session.query(EventRow).count() == 0
        names = {r.name for r in session.query(StrategyRow).all()}
        assert {"vwap_wave", "rsi_crypto"}.issubset(names)

    # Audit file written and parseable
    audits = list(tmp_path.glob("runtime/clean_slate_audit_*.jsonl"))
    assert len(audits) == 1
    lines = audits[0].read_text().splitlines()
    actions = [json.loads(line)["action"] for line in lines]
    assert "begin" in actions
    assert actions.count("cancel_order") == 2
    assert actions.count("submit_close") == 2
    assert actions.count("truncate") == len(sd._TRUNCATE_TABLES)
    assert "mysql_counts_after" in actions


# ── apply but broker won't flatten ───────────────────────────────────


def test_apply_with_unflattened_broker_does_not_truncate(
    monkeypatch, store, alpaca, capsys, tmp_path,
):
    """If positions remain after the poll deadline, MySQL is NOT truncated.

    This protects audit data — if Alpaca closes failed, we keep the MySQL
    history so the operator can investigate.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sd, "AlpacaClient", lambda *a, **k: alpaca)
    monkeypatch.setattr(sd, "MySQLStore", lambda *a, **k: store)
    _populate_state(store)

    # Broker stays non-flat across all polls
    alpaca.get_positions.return_value = [
        {"symbol": "AAPL", "qty": "10", "side": "long",
         "asset_class": "us_equity"},
    ]

    rc = sd.main([
        "--apply", "--confirm-account", _ACCOUNT_NUMBER,
        "--poll-seconds", "1",
    ])
    assert rc == 3

    # MySQL NOT truncated
    with Session(store._engine) as session:
        assert session.query(PositionRow).count() == 1
        assert session.query(TradeRow).count() == 1

    # Audit file still written so operator can debug
    audits = list(tmp_path.glob("runtime/clean_slate_audit_*.jsonl"))
    assert len(audits) == 1
    actions = [
        json.loads(line)["action"] for line in audits[0].read_text().splitlines()
    ]
    assert "broker_not_flat" in actions
    assert "truncate" not in actions  # never reached truncate phase


# ── close-order side mapping ─────────────────────────────────────────


def test_alpaca_side_to_close_inverts_position_side():
    assert sd._alpaca_side_to_close("long") == "sell"
    assert sd._alpaca_side_to_close("short") == "buy"


# ── COID minting ─────────────────────────────────────────────────────


def test_make_exit_coid_uses_role_exit_and_cleanslate_setup():
    coid = sd._make_exit_coid("AAPL")
    parsed = parse_client_order_id(coid)
    assert parsed is not None
    assert parsed["role"] == "exit"
    assert parsed["strategy"] == "operator"
    assert parsed["setup"] == "cleanslate"
    assert parsed["symbol"] == "AAPL"
