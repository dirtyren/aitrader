"""Integration test against the real MySQL test DB.

Mirrors the project's existing pattern of testing against a live MySQL
service (per the broker-position reconciliation work). Requires
MYSQL_HOST etc to point at a reachable MySQL — in CI this is the
docker-compose `mysql` service.
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone, timedelta
from decimal import Decimal

import pandas as pd
import pytest
from sqlalchemy import text

from ui.data.db import get_engine
from ui.data.trades_repo import get_closed_trades


pytestmark = pytest.mark.skipif(
    os.environ.get("SKIP_DB_TESTS") == "1",
    reason="DB-backed test; set SKIP_DB_TESTS=1 to skip locally",
)


def _seed_strategy(name: str) -> int:
    eng = get_engine()
    with eng.begin() as conn:
        conn.execute(text("INSERT IGNORE INTO strategies (name) VALUES (:n)"), {"n": name})
        row = conn.execute(text("SELECT id FROM strategies WHERE name=:n"), {"n": name}).one()
        return row[0]


def _insert_trade(strategy_id: int, *, symbol: str, closed_at: datetime,
                   pnl: float, R: float, setup: str = "vwap_bounce") -> None:
    eng = get_engine()
    with eng.begin() as conn:
        conn.execute(text("""
            INSERT INTO trades (strategy_id, symbol, asset_class, setup_name, side, qty,
                                entry_px, exit_px, pnl_usd, R_realized, close_reason,
                                opened_at, closed_at, bars_held)
            VALUES (:sid, :sym, 'equity', :setup, 'long', 1.0,
                    100.0, :exit, :pnl, :R, 'target',
                    :opened, :closed, 5)
        """), {
            "sid": strategy_id, "sym": symbol, "setup": setup,
            "exit": 100.0 + pnl, "pnl": pnl, "R": R,
            "opened": closed_at - timedelta(hours=1), "closed": closed_at,
        })


@pytest.fixture
def isolated_strategy():
    name = f"test_{uuid.uuid4().hex[:8]}"
    sid = _seed_strategy(name)
    yield name, sid
    eng = get_engine()
    with eng.begin() as conn:
        conn.execute(text("DELETE FROM trades WHERE strategy_id=:s"), {"s": sid})
        conn.execute(text("DELETE FROM positions WHERE strategy_id=:s"), {"s": sid})
        conn.execute(text("DELETE FROM strategies WHERE id=:s"), {"s": sid})


def test_get_closed_trades_filters_by_strategy_and_window(isolated_strategy):
    name, sid = isolated_strategy
    in_window = datetime(2026, 5, 15, 14, 0, tzinfo=timezone.utc)
    out_of_window = datetime(2026, 4, 1, 14, 0, tzinfo=timezone.utc)
    _insert_trade(sid, symbol="AAPL", closed_at=in_window, pnl=10.0, R=1.0)
    _insert_trade(sid, symbol="AAPL", closed_at=out_of_window, pnl=999.0, R=10.0)

    start = datetime(2026, 5, 1, tzinfo=timezone.utc)
    end = datetime(2026, 5, 31, tzinfo=timezone.utc)
    df = get_closed_trades(name, start, end)

    assert isinstance(df, pd.DataFrame)
    assert len(df) == 1
    assert float(df.iloc[0]["pnl_usd"]) == 10.0


def test_get_closed_trades_unknown_strategy_returns_empty():
    df = get_closed_trades("does_not_exist_xyz", datetime(2026, 1, 1, tzinfo=timezone.utc),
                                                   datetime(2026, 12, 31, tzinfo=timezone.utc))
    assert df.empty
