"""Tests for ui.data.strategy_admin.get_admin_view — period filtering + KPI merge."""
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
from ui.data import strategy_admin


@pytest.fixture
def store(monkeypatch):
    """In-memory MySQLStore plus a stub get_closed_trades that reads from
    the same engine the store is using. The real get_closed_trades opens
    a separate engine via ui.data.db.get_engine, which is unsuitable here.
    """
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    s = MySQLStore.__new__(MySQLStore)
    s._engine = engine
    s.strategy_name = "vwap_wave"
    s._strategy_id = None
    s._log = logging.getLogger("test_admin_view")

    with Session(engine) as session:
        session.add(StrategyRow(name="vwap_wave"))
        session.add(StrategyRow(name="orb"))
        session.commit()

    import pandas as pd
    from ui.data import strategy_admin as mod

    def fake_get_closed_trades(strategy, start, end):
        with Session(engine) as session:
            sid_row = session.query(StrategyRow).filter(
                StrategyRow.name == strategy,
            ).one_or_none()
            if sid_row is None:
                return pd.DataFrame()
            rows = session.query(TradeRow).filter(
                TradeRow.strategy_id == sid_row.id,
                TradeRow.closed_at >= start,
                TradeRow.closed_at < end,
            ).all()
            return pd.DataFrame([
                {
                    "pnl_usd": float(r.pnl_usd),
                    "R_realized": float(r.R_realized),
                    "closed_at": r.closed_at,
                    "bars_held": r.bars_held,
                    "setup_name": r.setup_name,
                }
                for r in rows
            ])

    monkeypatch.setattr(mod, "get_closed_trades", fake_get_closed_trades)
    return s


def _add_trade(engine, sid: int, symbol: str, pnl: float, r: float, when: datetime):
    with Session(engine) as session:
        session.add(TradeRow(
            strategy_id=sid, symbol=symbol, asset_class="crypto",
            setup_name="x", side="long",
            qty=Decimal("1"), entry_px=Decimal("100"),
            exit_px=Decimal("100"), pnl_usd=Decimal(str(pnl)),
            R_realized=Decimal(str(r)), close_reason="target",
            opened_at=when, closed_at=when, bars_held=1,
        ))
        session.commit()


def test_period_filters_trades_for_kpi_merge(store):
    """P&L / Win rate / Sharpe / Max DD / Avg R must reflect [start, end)."""
    sid_v = next(
        r.id for r in Session(store._engine).query(StrategyRow)
        if r.name == "vwap_wave"
    )
    now = datetime.now(timezone.utc).replace(microsecond=0)
    in_window = now - timedelta(days=1)
    out_of_window = now - timedelta(days=30)

    _add_trade(store._engine, sid_v, "A", pnl=10.0, r=1.0, when=in_window)
    _add_trade(store._engine, sid_v, "B", pnl=-5.0, r=-0.5, when=in_window - timedelta(days=1))
    _add_trade(store._engine, sid_v, "C", pnl=999.0, r=10.0, when=out_of_window)

    start = now - timedelta(days=7)
    end = now + timedelta(days=1)
    df = strategy_admin.get_admin_view(store, start, end)
    row = df[df["name"] == "vwap_wave"].iloc[0]

    assert row["period_pnl"] == pytest.approx(5.0)            # 10 + -5
    assert row["period_trade_count"] == 2
    assert row["period_win_rate"] == pytest.approx(0.5)       # 1 win of 2
    assert row["period_avg_r"] == pytest.approx(0.25)         # mean(1, -0.5)
    assert row["period_max_dd"] == pytest.approx(-5.0)        # peak 10 -> 5


def test_returns_none_metrics_for_empty_period(store):
    """Strategy with no in-window trades: KPIs are None / 0 (not crashes)."""
    now = datetime.now(timezone.utc).replace(microsecond=0)
    start = now - timedelta(days=7)
    end = now + timedelta(days=1)

    df = strategy_admin.get_admin_view(store, start, end)
    orb_row = df[df["name"] == "orb"].iloc[0]

    assert orb_row["period_trade_count"] == 0
    assert orb_row["period_pnl"] == 0.0
    assert orb_row["period_win_rate"] is None
    assert orb_row["period_sharpe"] is None
    assert orb_row["period_avg_r"] is None
    assert orb_row["period_max_dd"] == 0.0


def test_sharpe_none_when_fewer_than_two_days(store):
    """Sharpe needs >=2 distinct trading days — single-day period returns None."""
    sid_v = next(
        r.id for r in Session(store._engine).query(StrategyRow)
        if r.name == "vwap_wave"
    )
    # Fixed mid-day UTC so trade timestamps cannot straddle midnight.
    fixed = datetime(2026, 5, 30, 12, 0, 0, tzinfo=timezone.utc)
    _add_trade(store._engine, sid_v, "A", pnl=10.0, r=1.0, when=fixed)
    _add_trade(store._engine, sid_v, "B", pnl=20.0, r=2.0, when=fixed - timedelta(hours=1))

    start = fixed - timedelta(hours=12)
    end = fixed + timedelta(hours=1)
    df = strategy_admin.get_admin_view(store, start, end)
    row = df[df["name"] == "vwap_wave"].iloc[0]

    assert row["period_trade_count"] == 2
    assert row["period_sharpe"] is None       # only one day in the window


def test_preserves_columns_from_sql_aggregator(store):
    """Existing columns (open_count, today_pnl, total_pnl, etc.) still present."""
    now = datetime.now(timezone.utc).replace(microsecond=0)
    df = strategy_admin.get_admin_view(store, now - timedelta(days=1), now + timedelta(days=1))
    expected = {"id", "name", "state", "enabled", "open_count",
                "today_pnl", "total_pnl", "win_rate", "trade_count",
                "period_pnl", "period_win_rate", "period_sharpe",
                "period_max_dd", "period_avg_r", "period_trade_count"}
    assert expected.issubset(set(df.columns))
