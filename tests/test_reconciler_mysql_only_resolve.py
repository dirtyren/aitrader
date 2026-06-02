"""Auto-resolve mysql_only strikes whose entry order is still unfilled.

Background: order_executor writes the MySQL position row at submit time,
so a limit-bracket whose parent never fills leaves a phantom open row.
The reconciler raises mysql_only on it; this auto-resolve path closes
ONLY the safe sub-case (entry order still in new/accepted/held with zero
filled qty) — anything else is left for the operator.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from reconciler.config import ReconcilerConfig
from reconciler.invariant import Anomaly
from reconciler.main import auto_resolve_mysql_only_entry_never_filled
from state.mysql_store import (
    Base,
    EventRow,
    MySQLStore,
    PositionRow,
    StrategyRow,
    StrikeRow,
    TradeRow,
)


@pytest.fixture
def store():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    s = MySQLStore.__new__(MySQLStore)
    s._engine = engine
    s.strategy_name = "reconciler-equity"
    s._log = logging.getLogger("test_mysql_only_resolve")
    with Session(engine) as session:
        session.add(StrategyRow(name="rsi_equity_trader"))
        session.commit()
        s._strategy_id = session.query(StrategyRow).one().id
    return s


def _cfg(shadow=False, threshold=3, min_gap=60):
    return ReconcilerConfig(
        interval_s=30, strike_threshold=threshold, strike_min_gap_s=min_gap,
        qty_eps=1e-6, shadow_mode=shadow,
        state_file_path="/tmp/state.json",
        heartbeat_stale_after_s=300,
        auto_close_max_notional_usd=190_000.0,
        auto_close_dust_usd=1.0,
        asset_class="equity",
    )


def _entry_coid(symbol="NFLX", setup="rsi_oversold",
                strategy="rsi_equity_trader", uuid="deadbeef"):
    return f"aitrader__{strategy}__{setup}__{symbol}__entry__{uuid}"


def _seed_open_position(
    session, store, *, symbol="NFLX", setup="rsi_oversold",
    qty=2.0, entry_px=73.87, asset_class="equity",
    coid=None, opened_at=None,
) -> PositionRow:
    coid = coid or _entry_coid(symbol=symbol, setup=setup)
    opened_at = opened_at or datetime(2026, 6, 2, 12, 0, tzinfo=timezone.utc)
    row = PositionRow(
        strategy_id=store._strategy_id, setup_name=setup, symbol=symbol,
        side="long", qty=qty, entry_px=entry_px, opened_at=opened_at,
        asset_class=asset_class, status="open",
        client_order_id=coid, legacy_untagged=0,
    )
    session.add(row)
    session.flush()
    return row


def _seed_strike(session, *, strategy_id: int, symbol: str, count: int,
                 mysql_qty: float, now: datetime,
                 asset_class: str | None = "equity") -> StrikeRow:
    row = StrikeRow(
        key=f"mysql_only:{strategy_id}:{symbol}",
        direction="mysql_only",
        strategy_id=strategy_id,
        symbol=symbol,
        asset_class=asset_class,
        strike_count=count,
        first_seen_at=now - timedelta(minutes=count * 2),
        last_seen_at=now - timedelta(seconds=120),
        last_observed_state={"mysql_qty": mysql_qty, "broker_qty": 0.0},
        resolved=False,
    )
    session.add(row)
    session.flush()
    return row


def _mysql_only_anomaly(strategy_id: int, symbol="NFLX",
                        mysql_qty=2.0, asset_class="equity") -> Anomaly:
    return Anomaly(
        direction="mysql_only",
        symbol=symbol,
        strategy_id=strategy_id,
        snapshot={"mysql_qty": mysql_qty, "broker_qty": 0.0},
        asset_class=asset_class,
    )


def _accepted_entry_order(*, coid, symbol="NFLX",
                          status="accepted", filled_qty="0"):
    return {
        "id": f"alp-{coid[-8:]}",
        "client_order_id": coid,
        "symbol": symbol,
        "status": status,
        "filled_qty": filled_qty,
    }


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_at_threshold_cancels_entry_and_closes_mysql_row(store):
    cfg = _cfg(threshold=3)
    now = datetime(2026, 6, 2, 14, 0, tzinfo=timezone.utc)
    coid = _entry_coid()

    alpaca = MagicMock()
    alpaca.list_orders.return_value = [_accepted_entry_order(coid=coid)]
    alpaca.cancel_order.return_value = True

    with Session(store._engine) as session:
        _seed_open_position(session, store, coid=coid)
        _seed_strike(session, strategy_id=store._strategy_id,
                     symbol="NFLX", count=3, mysql_qty=2.0, now=now)
        session.commit()

        anomaly = _mysql_only_anomaly(store._strategy_id)
        resolved = auto_resolve_mysql_only_entry_never_filled(
            alpaca=alpaca, store=store, session=session,
            anomalies=[anomaly], recent_fills=[],
            cfg=cfg, now=now, asset_class="equity",
        )
        session.commit()

    assert resolved == 1
    alpaca.cancel_order.assert_called_once_with(f"alp-{coid[-8:]}")

    with Session(store._engine) as session:
        # MySQL row closed with the right reason.
        pos = session.query(PositionRow).filter(
            PositionRow.symbol == "NFLX",
        ).one()
        assert pos.status == "closed"
        assert pos.close_reason == "entry_never_filled"
        assert float(pos.exit_px) == pytest.approx(73.87)

        # Trade row archived with zero PnL.
        trade = session.query(TradeRow).filter(
            TradeRow.symbol == "NFLX",
        ).one()
        assert trade.close_reason == "entry_never_filled"
        assert float(trade.pnl_usd) == pytest.approx(0.0)

        # Strike resolved.
        strike = session.query(StrikeRow).one()
        assert strike.resolved is True
        assert strike.resolved_reason == "auto_close_entry_never_filled"

        # Audit event emitted.
        events = [r.type for r in session.query(EventRow).all()]
        assert "auto_close_entry_never_filled" in events


# ---------------------------------------------------------------------------
# Guards: don't fire on race / not-yet-confirmed conditions
# ---------------------------------------------------------------------------


def test_below_threshold_does_not_resolve(store):
    cfg = _cfg(threshold=3)
    now = datetime(2026, 6, 2, 14, 0, tzinfo=timezone.utc)
    alpaca = MagicMock()

    with Session(store._engine) as session:
        _seed_open_position(session, store)
        _seed_strike(session, strategy_id=store._strategy_id,
                     symbol="NFLX", count=1, mysql_qty=2.0, now=now)
        session.commit()
        anomaly = _mysql_only_anomaly(store._strategy_id)
        resolved = auto_resolve_mysql_only_entry_never_filled(
            alpaca=alpaca, store=store, session=session,
            anomalies=[anomaly], recent_fills=[],
            cfg=cfg, now=now, asset_class="equity",
        )
        session.commit()

    assert resolved == 0
    alpaca.cancel_order.assert_not_called()
    alpaca.list_orders.assert_not_called()


def test_skips_when_recent_fill_carries_same_coid(store):
    """If apply_tagged_fill is going to close the row this same cycle,
    the auto-resolve must stay out of its way."""
    cfg = _cfg(threshold=3)
    now = datetime(2026, 6, 2, 14, 0, tzinfo=timezone.utc)
    coid = _entry_coid()

    alpaca = MagicMock()

    with Session(store._engine) as session:
        _seed_open_position(session, store, coid=coid)
        _seed_strike(session, strategy_id=store._strategy_id,
                     symbol="NFLX", count=3, mysql_qty=2.0, now=now)
        session.commit()
        anomaly = _mysql_only_anomaly(store._strategy_id)
        resolved = auto_resolve_mysql_only_entry_never_filled(
            alpaca=alpaca, store=store, session=session,
            anomalies=[anomaly],
            recent_fills=[{
                "id": "alp-fill-1", "client_order_id": coid,
                "filled_qty": "2", "filled_avg_price": "73.87",
                "symbol": "NFLX", "side": "buy", "asset_class": "us_equity",
                "filled_at": "2026-06-02T13:55:00Z", "status": "filled",
            }],
            cfg=cfg, now=now, asset_class="equity",
        )
        session.commit()

    assert resolved == 0
    alpaca.list_orders.assert_not_called()
    alpaca.cancel_order.assert_not_called()


def test_skips_when_no_open_mysql_row(store):
    """Race: row already closed between invariant check and now."""
    cfg = _cfg(threshold=3)
    now = datetime(2026, 6, 2, 14, 0, tzinfo=timezone.utc)
    alpaca = MagicMock()

    with Session(store._engine) as session:
        _seed_strike(session, strategy_id=store._strategy_id,
                     symbol="NFLX", count=3, mysql_qty=2.0, now=now)
        session.commit()
        anomaly = _mysql_only_anomaly(store._strategy_id)
        resolved = auto_resolve_mysql_only_entry_never_filled(
            alpaca=alpaca, store=store, session=session,
            anomalies=[anomaly], recent_fills=[],
            cfg=cfg, now=now, asset_class="equity",
        )
        session.commit()

    assert resolved == 0
    alpaca.cancel_order.assert_not_called()


# ---------------------------------------------------------------------------
# Defensive: leave the strike for the operator on anything ambiguous
# ---------------------------------------------------------------------------


def test_ambiguous_setup_emits_event_and_skips(store):
    """Two open rows on the same (strategy, symbol) pair — can't pick one
    safely; emit event + leave for operator."""
    cfg = _cfg(threshold=3)
    now = datetime(2026, 6, 2, 14, 0, tzinfo=timezone.utc)
    alpaca = MagicMock()

    with Session(store._engine) as session:
        _seed_open_position(session, store, setup="rsi_oversold",
                            coid=_entry_coid(setup="rsi_oversold"))
        _seed_open_position(session, store, setup="rsi_overbought",
                            coid=_entry_coid(setup="rsi_overbought"))
        _seed_strike(session, strategy_id=store._strategy_id,
                     symbol="NFLX", count=3, mysql_qty=4.0, now=now)
        session.commit()
        anomaly = _mysql_only_anomaly(store._strategy_id, mysql_qty=4.0)
        resolved = auto_resolve_mysql_only_entry_never_filled(
            alpaca=alpaca, store=store, session=session,
            anomalies=[anomaly], recent_fills=[],
            cfg=cfg, now=now, asset_class="equity",
        )
        session.commit()

    assert resolved == 0
    alpaca.cancel_order.assert_not_called()
    with Session(store._engine) as session:
        events = [r.type for r in session.query(EventRow).all()]
        assert "mysql_only_ambiguous_setup" in events
        # Both MySQL rows still open.
        open_count = session.query(PositionRow).filter(
            PositionRow.status == "open",
        ).count()
        assert open_count == 2


def test_partial_fill_emits_event_and_skips(store):
    """Order shows filled_qty>0 — that's a real divergence, not an
    unfilled limit. Operator must reconcile."""
    cfg = _cfg(threshold=3)
    now = datetime(2026, 6, 2, 14, 0, tzinfo=timezone.utc)
    coid = _entry_coid()

    alpaca = MagicMock()
    alpaca.list_orders.return_value = [
        _accepted_entry_order(
            coid=coid, status="partially_filled", filled_qty="1",
        ),
    ]

    with Session(store._engine) as session:
        _seed_open_position(session, store, coid=coid)
        _seed_strike(session, strategy_id=store._strategy_id,
                     symbol="NFLX", count=3, mysql_qty=2.0, now=now)
        session.commit()
        anomaly = _mysql_only_anomaly(store._strategy_id)
        resolved = auto_resolve_mysql_only_entry_never_filled(
            alpaca=alpaca, store=store, session=session,
            anomalies=[anomaly], recent_fills=[],
            cfg=cfg, now=now, asset_class="equity",
        )
        session.commit()

    assert resolved == 0
    alpaca.cancel_order.assert_not_called()
    with Session(store._engine) as session:
        events = [r.type for r in session.query(EventRow).all()]
        assert "mysql_only_partially_filled" in events
        pos = session.query(PositionRow).one()
        assert pos.status == "open"


def test_entry_coid_missing_emits_event_and_skips(store):
    """Order canceled out-of-band: not in open OR closed lists."""
    cfg = _cfg(threshold=3)
    now = datetime(2026, 6, 2, 14, 0, tzinfo=timezone.utc)
    alpaca = MagicMock()
    alpaca.list_orders.return_value = []  # both open and closed lookups

    with Session(store._engine) as session:
        _seed_open_position(session, store)
        _seed_strike(session, strategy_id=store._strategy_id,
                     symbol="NFLX", count=3, mysql_qty=2.0, now=now)
        session.commit()
        anomaly = _mysql_only_anomaly(store._strategy_id)
        resolved = auto_resolve_mysql_only_entry_never_filled(
            alpaca=alpaca, store=store, session=session,
            anomalies=[anomaly], recent_fills=[],
            cfg=cfg, now=now, asset_class="equity",
        )
        session.commit()

    assert resolved == 0
    alpaca.cancel_order.assert_not_called()
    with Session(store._engine) as session:
        events = [r.type for r in session.query(EventRow).all()]
        assert "mysql_only_entry_coid_missing" in events
        pos = session.query(PositionRow).one()
        assert pos.status == "open"


def test_filled_at_broker_in_closed_orders_emits_event_and_skips(store):
    """Order is in `closed` orders with status=filled — we missed the fill
    in our `recent_fills` window. Don't auto-cancel; operator must
    reconcile against the actual fill."""
    cfg = _cfg(threshold=3)
    now = datetime(2026, 6, 2, 14, 0, tzinfo=timezone.utc)
    coid = _entry_coid()

    alpaca = MagicMock()
    # First call (status=open) returns nothing; second (status=closed)
    # returns a filled match.
    alpaca.list_orders.side_effect = [
        [],
        [{
            "id": f"alp-{coid[-8:]}",
            "client_order_id": coid,
            "status": "filled",
            "filled_qty": "2",
            "symbol": "NFLX",
        }],
    ]

    with Session(store._engine) as session:
        _seed_open_position(session, store, coid=coid)
        _seed_strike(session, strategy_id=store._strategy_id,
                     symbol="NFLX", count=3, mysql_qty=2.0, now=now)
        session.commit()
        anomaly = _mysql_only_anomaly(store._strategy_id)
        resolved = auto_resolve_mysql_only_entry_never_filled(
            alpaca=alpaca, store=store, session=session,
            anomalies=[anomaly], recent_fills=[],
            cfg=cfg, now=now, asset_class="equity",
        )
        session.commit()

    assert resolved == 0
    alpaca.cancel_order.assert_not_called()
    with Session(store._engine) as session:
        events = [r.type for r in session.query(EventRow).all()]
        assert "mysql_only_filled_at_broker" in events
        pos = session.query(PositionRow).one()
        assert pos.status == "open"


def test_cancel_failure_keeps_strike_unresolved(store):
    """If cancel_order raises (most likely the order flipped to filled
    between check and cancel), MySQL row stays open and strike unresolved."""
    cfg = _cfg(threshold=3)
    now = datetime(2026, 6, 2, 14, 0, tzinfo=timezone.utc)
    coid = _entry_coid()

    alpaca = MagicMock()
    alpaca.list_orders.return_value = [_accepted_entry_order(coid=coid)]
    alpaca.cancel_order.side_effect = RuntimeError("order is not open")

    with Session(store._engine) as session:
        _seed_open_position(session, store, coid=coid)
        _seed_strike(session, strategy_id=store._strategy_id,
                     symbol="NFLX", count=3, mysql_qty=2.0, now=now)
        session.commit()
        anomaly = _mysql_only_anomaly(store._strategy_id)
        resolved = auto_resolve_mysql_only_entry_never_filled(
            alpaca=alpaca, store=store, session=session,
            anomalies=[anomaly], recent_fills=[],
            cfg=cfg, now=now, asset_class="equity",
        )
        session.commit()

    assert resolved == 0
    with Session(store._engine) as session:
        events = [r.type for r in session.query(EventRow).all()]
        assert "mysql_only_cancel_failed" in events
        pos = session.query(PositionRow).one()
        assert pos.status == "open"
        strike = session.query(StrikeRow).one()
        assert strike.resolved is False


# ---------------------------------------------------------------------------
# Shadow mode
# ---------------------------------------------------------------------------


def test_shadow_mode_never_mutates(store):
    cfg = _cfg(shadow=True, threshold=3)
    now = datetime(2026, 6, 2, 14, 0, tzinfo=timezone.utc)
    coid = _entry_coid()

    alpaca = MagicMock()
    alpaca.list_orders.return_value = [_accepted_entry_order(coid=coid)]

    with Session(store._engine) as session:
        _seed_open_position(session, store, coid=coid)
        _seed_strike(session, strategy_id=store._strategy_id,
                     symbol="NFLX", count=3, mysql_qty=2.0, now=now)
        session.commit()
        anomaly = _mysql_only_anomaly(store._strategy_id)
        resolved = auto_resolve_mysql_only_entry_never_filled(
            alpaca=alpaca, store=store, session=session,
            anomalies=[anomaly], recent_fills=[],
            cfg=cfg, now=now, asset_class="equity",
        )
        session.commit()

    assert resolved == 0
    alpaca.cancel_order.assert_not_called()
    alpaca.list_orders.assert_not_called()
    with Session(store._engine) as session:
        pos = session.query(PositionRow).one()
        assert pos.status == "open"
