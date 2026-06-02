"""Duplicate-open-row defenses in MySQLStore.

Two open rows that differ only in symbol slash form (DOGE/USD vs
DOGEUSD) for the same (strategy_id, setup_name) used to crash the
trader at startup in load_open_positions. This test file pins the
four guards we put in place:

- Write-time normalization: position_opened / insert_position_from_fill
  / insert_adopted_position all persist the broker-flat form.
- Defensive load: load_open_positions logs and skips a duplicate
  instead of raising.
- Close-time consolidation: position_closed handles `count > 1` with
  one TradeRow per closed row.
- Startup one-shot cleanup: ensure_schema closes the older of any
  duplicate pair at exit_px=entry_px and archives a TradeRow.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from state.mysql_store import (
    Base,
    MySQLStore,
    PositionRow,
    StrategyRow,
    TradeRow,
)
from state.position_book import OpenPosition


@pytest.fixture
def store():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    s = MySQLStore.__new__(MySQLStore)
    s._engine = engine
    s.strategy_name = "ib_crypto_trader"
    s._log = logging.getLogger("test_dup_open_rows")
    with Session(engine) as session:
        session.add(StrategyRow(name="ib_crypto_trader"))
        session.commit()
        s._strategy_id = session.query(StrategyRow).one().id
    return s


def _seed_dup_open_rows(store) -> tuple[int, int]:
    """Seed two open rows that differ ONLY in symbol form. Returns
    (older_id, newer_id)."""
    base = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    older = PositionRow(
        strategy_id=store._strategy_id, setup_name="initial_balance",
        symbol="DOGE/USD", asset_class="crypto", side="long",
        qty=Decimal("100"), entry_px=Decimal("0.10"),
        opened_at=base - timedelta(seconds=5),
        client_order_id="aitrader__ib_crypto__initial_balance__DOGEUSD__entry__a1",
        legacy_untagged=0, fill_confirmed=True, status="open",
    )
    newer = PositionRow(
        strategy_id=store._strategy_id, setup_name="initial_balance",
        symbol="DOGEUSD", asset_class="crypto", side="long",
        qty=Decimal("100"), entry_px=Decimal("0.10"),
        opened_at=base, client_order_id=older.client_order_id,
        legacy_untagged=0, fill_confirmed=True, status="open",
    )
    with Session(store._engine) as session:
        session.add_all([older, newer])
        session.commit()
        return older.id, newer.id


# ---------------------------------------------------------------------------
# Write-time normalization
# ---------------------------------------------------------------------------


def test_position_opened_persists_flat_symbol(store):
    pos = OpenPosition(
        symbol="DOGE/USD", setup="initial_balance", side="long",
        qty=100, entry_px=0.10, stop_px=0.09, target_px=0.12,
        opened_at=datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc),
        order_id="alp-1",
    )
    store.position_opened(pos, asset_class="crypto")

    with Session(store._engine) as session:
        row = session.query(PositionRow).one()
        assert row.symbol == "DOGEUSD"


def test_insert_position_from_fill_persists_flat_symbol(store):
    """A buggy caller could pass slash form here too — defend in depth."""
    store.insert_position_from_fill(
        strategy_id=store._strategy_id, setup_name="initial_balance",
        symbol="DOGE/USD",  # caller-side slash that should NOT make it through
        side="long", qty=100, entry_px=0.10,
        opened_at=datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc),
        asset_class="crypto",
        client_order_id="aitrader__ib_crypto__initial_balance__DOGEUSD__entry__a1",
    )
    with Session(store._engine) as session:
        row = session.query(PositionRow).one()
        assert row.symbol == "DOGEUSD"


# ---------------------------------------------------------------------------
# Defensive load
# ---------------------------------------------------------------------------


def test_load_open_positions_skips_duplicate(store, caplog):
    _seed_dup_open_rows(store)

    with caplog.at_level(logging.ERROR, logger="test_dup_open_rows"):
        book = store.load_open_positions()

    # PositionBook only carries one entry — duplicates can't coexist there.
    assert book.count() == 1
    # The skip is loud so an operator can see it.
    assert any("MYSQL_DUPLICATE_OPEN_ROW" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Close-time consolidation
# ---------------------------------------------------------------------------


def test_position_closed_consolidates_both_rows(store):
    older_id, newer_id = _seed_dup_open_rows(store)

    closed_at = datetime(2026, 6, 1, 13, 0, tzinfo=timezone.utc)
    result = store.position_closed(
        symbol="DOGE/USD", exit_px=0.11,
        close_reason="broker_exit",
        setup_name="initial_balance",
        strategy_id=store._strategy_id,
        closed_at=closed_at,
    )

    assert result is not None
    with Session(store._engine) as session:
        # Both rows are now closed.
        statuses = {
            r.id: r.status for r in session.query(PositionRow).all()
        }
        assert statuses == {older_id: "closed", newer_id: "closed"}
        # Each row got its own TradeRow archive — audit trail per row.
        trades = session.query(TradeRow).all()
        assert len(trades) == 2


# ---------------------------------------------------------------------------
# Startup one-shot cleanup
# ---------------------------------------------------------------------------


def test_ensure_schema_consolidates_existing_duplicate(store):
    older_id, newer_id = _seed_dup_open_rows(store)

    # ensure_schema is the public entry point; the migration block runs
    # ALTER statements that are no-ops on SQLite — but the duplicate
    # cleanup is the one we care about here.
    store.ensure_schema()

    with Session(store._engine) as session:
        older = session.get(PositionRow, older_id)
        newer = session.get(PositionRow, newer_id)
        assert older.status == "closed"
        assert older.close_reason == "duplicate_consolidation"
        assert float(older.exit_px) == pytest.approx(float(older.entry_px))
        assert float(older.pnl_usd) == 0.0
        # The newer row is left alone — broker truth.
        assert newer.status == "open"
        # Audit row archived.
        trades = session.query(TradeRow).filter(
            TradeRow.close_reason == "duplicate_consolidation",
        ).all()
        assert len(trades) == 1


def test_ensure_schema_is_idempotent_on_clean_db(store):
    """No duplicates → no rows touched, no audit rows created."""
    pos = OpenPosition(
        symbol="DOGE/USD", setup="initial_balance", side="long",
        qty=100, entry_px=0.10, stop_px=0.09, target_px=0.12,
        opened_at=datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc),
        order_id="alp-1",
    )
    store.position_opened(pos, asset_class="crypto")

    store.ensure_schema()
    store.ensure_schema()  # second pass exercises idempotency

    with Session(store._engine) as session:
        rows = session.query(PositionRow).all()
        assert len(rows) == 1
        assert rows[0].status == "open"
        assert session.query(TradeRow).count() == 0
