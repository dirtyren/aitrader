"""Tests for scripts/backfill_legacy_coids.py."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import scripts.backfill_legacy_coids as cli
from broker.client_order_id import parse_client_order_id
from state.mysql_store import (
    Base,
    EventRow,
    MySQLStore,
    PositionRow,
    StrategyRow,
)


@pytest.fixture
def store():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    s = MySQLStore.__new__(MySQLStore)
    s._engine = engine
    s.strategy_name = "operator"
    s._log = logging.getLogger("test_backfill")
    with Session(engine) as session:
        session.add_all([
            StrategyRow(name="vwap_wave"),
            StrategyRow(name="rsi_crypto_trader"),
        ])
        session.commit()
        rows = session.query(StrategyRow).order_by(StrategyRow.id).all()
    s._strategy_id = rows[0].id
    s._other_strategy_id = rows[1].id
    return s


def _add_row(store, *, strategy_id, symbol, setup, side="long",
             coid=None, legacy=True, status="open"):
    """Insert a position row directly."""
    with Session(store._engine) as session:
        row = PositionRow(
            strategy_id=strategy_id,
            symbol=symbol, setup_name=setup, side=side,
            qty=Decimal("1"), entry_px=Decimal("100"),
            asset_class="equity",
            order_id="", client_order_id=coid,
            stop_order_id=None, breakeven_moved=False, bars_held=0,
            adopted=False, legacy_untagged=legacy,
            status=status,
            opened_at=datetime(2026, 5, 28, 14, 0, tzinfo=timezone.utc),
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        return row.id


def test_candidates_returns_only_legacy_untagged_open_rows_with_null_coid(store):
    _add_row(store, strategy_id=store._strategy_id, symbol="AAPL",
             setup="vwap_bounce", legacy=True, coid=None)
    _add_row(store, strategy_id=store._strategy_id, symbol="MSFT",
             setup="vwap_bounce", legacy=False, coid=None)  # not legacy
    _add_row(store, strategy_id=store._strategy_id, symbol="JPM",
             setup="vwap_bounce", legacy=True,
             coid="aitrader__x__y__JPM__entry__abcd1234")  # already has coid
    _add_row(store, strategy_id=store._strategy_id, symbol="OLD",
             setup="vwap_bounce", legacy=True, coid=None,
             status="closed")  # not open
    with Session(store._engine) as session:
        candidates = cli._candidates(session)
    assert len(candidates) == 1
    row, name = candidates[0]
    assert row.symbol == "AAPL"
    assert name == "vwap_wave"


def test_dry_run_writes_nothing(store, monkeypatch, capsys):
    _add_row(store, strategy_id=store._strategy_id, symbol="AAPL",
             setup="vwap_bounce", legacy=True, coid=None)

    # Patch the MySQLStore constructor inside the script so it uses our store
    monkeypatch.setattr(cli, "MySQLStore", lambda *a, **k: store)

    rc = cli.main(["--dry-run"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "AAPL" in out
    assert "dry-run" in out

    with Session(store._engine) as session:
        row = session.query(PositionRow).one()
        # Unchanged
        assert row.client_order_id is None
        assert row.legacy_untagged is True
        assert session.query(EventRow).count() == 0


def test_apply_stamps_coid_clears_legacy_writes_event(store, monkeypatch, capsys):
    _add_row(store, strategy_id=store._strategy_id, symbol="AAPL",
             setup="vwap_bounce", legacy=True, coid=None)
    _add_row(store, strategy_id=store._other_strategy_id, symbol="ETH/USD",
             setup="rsi_long", legacy=True, coid=None)

    monkeypatch.setattr(cli, "MySQLStore", lambda *a, **k: store)
    rc = cli.main(["--apply"])
    assert rc == 0

    with Session(store._engine) as session:
        rows = session.query(PositionRow).order_by(PositionRow.symbol).all()
        assert len(rows) == 2
        # Both rows now have parseable role=adopted COIDs
        for row in rows:
            assert row.client_order_id is not None
            assert row.legacy_untagged is False
            parsed = parse_client_order_id(row.client_order_id)
            assert parsed is not None
            assert parsed["role"] == "adopted"
            assert parsed["setup"] == row.setup_name
        # Strategy and symbol mapped through correctly
        aapl = next(r for r in rows if r.symbol == "AAPL")
        eth = next(r for r in rows if r.symbol == "ETH/USD")
        assert parse_client_order_id(aapl.client_order_id)["strategy"] == "vwap_wave"
        # ETHUSD because the COID sanitizer strips "/" from symbols
        assert parse_client_order_id(eth.client_order_id)["symbol"] == "ETHUSD"
        assert parse_client_order_id(eth.client_order_id)["strategy"] == "rsi_crypto_trader"
        # Audit events written, one per row
        events = session.query(EventRow).filter(
            EventRow.type == "operator_action"
        ).all()
        assert len(events) == 2
        for e in events:
            assert e.payload["operator_action"] == "backfill_legacy_coid"
            assert e.payload["operator_note"] == "backfill_legacy_coids.py --apply"


def test_apply_is_idempotent(store, monkeypatch):
    """Running apply twice does nothing the second time (no candidates left)."""
    _add_row(store, strategy_id=store._strategy_id, symbol="AAPL",
             setup="vwap_bounce", legacy=True, coid=None)
    monkeypatch.setattr(cli, "MySQLStore", lambda *a, **k: store)

    cli.main(["--apply"])
    rc = cli.main(["--apply"])
    assert rc == 0

    with Session(store._engine) as session:
        # Still exactly 1 row, still 1 event (no duplicate apply)
        assert session.query(PositionRow).count() == 1
        assert session.query(EventRow).filter(
            EventRow.type == "operator_action"
        ).count() == 1


def test_dry_run_and_apply_are_mutually_exclusive(store, monkeypatch, capsys):
    """argparse rejects both --dry-run and --apply at once."""
    monkeypatch.setattr(cli, "MySQLStore", lambda *a, **k: store)
    with pytest.raises(SystemExit):
        cli.main(["--dry-run", "--apply"])
    with pytest.raises(SystemExit):
        cli.main([])  # neither flag


def test_no_candidates_prints_message_and_exits_zero(store, monkeypatch, capsys):
    monkeypatch.setattr(cli, "MySQLStore", lambda *a, **k: store)
    rc = cli.main(["--apply"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "no legacy_untagged" in out.lower()
