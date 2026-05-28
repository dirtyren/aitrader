import os
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import text

from ui.data.db import get_engine
from ui.data.positions_repo import get_open


pytestmark = pytest.mark.skipif(
    os.environ.get("SKIP_DB_TESTS") == "1",
    reason="DB-backed test",
)


@pytest.fixture
def isolated_strategy():
    name = f"test_{uuid.uuid4().hex[:8]}"
    eng = get_engine()
    with eng.begin() as conn:
        conn.execute(text("INSERT INTO strategies (name) VALUES (:n)"), {"n": name})
        sid = conn.execute(text("SELECT id FROM strategies WHERE name=:n"), {"n": name}).one()[0]
    yield name, sid
    with eng.begin() as conn:
        conn.execute(text("DELETE FROM positions WHERE strategy_id=:s"), {"s": sid})
        conn.execute(text("DELETE FROM strategies WHERE id=:s"), {"s": sid})


def _insert_pos(sid, *, symbol, status, side="long"):
    eng = get_engine()
    with eng.begin() as conn:
        conn.execute(text("""
            INSERT INTO positions (strategy_id, symbol, asset_class, side, qty,
                                   entry_px, stop_px, target_px, setup_name,
                                   status, opened_at)
            VALUES (:sid, :sym, 'equity', :side, 1.0, 100.0, 99.0, 102.0,
                    'vwap_bounce', :status, :opened)
        """), {
            "sid": sid, "sym": symbol, "side": side, "status": status,
            "opened": datetime(2026, 5, 28, 14, 0, tzinfo=timezone.utc),
        })


def test_get_open_returns_only_open_positions_with_strategy_name(isolated_strategy):
    name, sid = isolated_strategy
    _insert_pos(sid, symbol="AAPL", status="open")
    _insert_pos(sid, symbol="MSFT", status="closed")
    df = get_open()
    rows = df[df["strategy"] == name]
    assert len(rows) == 1
    assert rows.iloc[0]["symbol"] == "AAPL"
    assert rows.iloc[0]["status"] == "open"
    assert {"strategy", "symbol", "side", "qty", "entry_px", "stop_px",
            "target_px", "setup_name", "asset_class", "opened_at"}.issubset(df.columns)
