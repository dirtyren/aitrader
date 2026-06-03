"""Auto-resolve path for qty_drift anomalies.

When MySQL Σ qty for a symbol disagrees with broker qty, the reconciler:
  - attributes the drift to a single open MySQL row by walking COIDs of
    recent fills, and
  - submits the right close on the broker (full close on broker<MySQL,
    surplus close on broker>MySQL), trimming MySQL only on the full-close
    branch.

Strike-gated by the same N-cycle rule as auto_close_broker_only.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from broker.client_order_id import Role, make_client_order_id
from reconciler.config import ReconcilerConfig
from reconciler.invariant import Anomaly
from reconciler.main import auto_resolve_qty_drift
from state.mysql_store import (
    Base,
    EventRow,
    MySQLStore,
    PositionRow,
    StrategyRow,
    StrikeRow,
)


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def store():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    s = MySQLStore.__new__(MySQLStore)
    s._engine = engine
    s.strategy_name = "reconciler"
    s._log = logging.getLogger("test_qty_drift")
    with Session(engine) as session:
        session.add(StrategyRow(name="vwap_wave"))
        session.add(StrategyRow(name="orb"))
        session.commit()
    return s


def _strategy_id(store: MySQLStore, name: str) -> int:
    with Session(store._engine) as session:
        return session.query(StrategyRow).filter(
            StrategyRow.name == name,
        ).one().id


def _cfg(shadow: bool = False, threshold: int = 3, min_gap: int = 60,
         dust_usd: float = 1.0):
    return ReconcilerConfig(
        interval_s=30, strike_threshold=threshold, strike_min_gap_s=min_gap,
        qty_eps=1e-6, shadow_mode=shadow,
        state_file_path="/tmp/state.json",
        heartbeat_stale_after_s=300,
        auto_close_max_notional_usd=190_000.0,
        auto_close_dust_usd=dust_usd,
    )


def _seed_strike(session: Session, symbol: str, count: int, *,
                 now: datetime, snapshot: dict | None = None) -> StrikeRow:
    row = StrikeRow(
        key=f"qty_drift:{symbol}",
        direction="qty_drift",
        strategy_id=None,
        symbol=symbol,
        strike_count=count,
        first_seen_at=now - timedelta(minutes=count * 2),
        last_seen_at=now - timedelta(seconds=120),
        last_observed_state=snapshot or {"mysql_sum": 100.0, "broker_qty": 80.0},
        resolved=False,
    )
    session.add(row)
    session.flush()
    return row


def _seed_open_position(
    session: Session, *, strategy_id: int, symbol: str, setup: str,
    qty: float, side: str = "long", asset_class: str = "equity",
    coid: str | None = None,
) -> PositionRow:
    row = PositionRow(
        strategy_id=strategy_id,
        symbol=symbol,
        asset_class=asset_class,
        side=side,
        qty=Decimal(str(qty)),
        entry_px=Decimal("100"),
        stop_px=None,
        target_px=None,
        initial_stop_px=None,
        setup_name=setup,
        order_id="entry-x",
        client_order_id=coid or make_client_order_id(
            "vwap_wave", setup, symbol.replace("/", ""), Role.ENTRY,
        ),
        stop_order_id=None,
        breakeven_moved=False,
        bars_held=0,
        adopted=False,
        status="open",
        opened_at=datetime(2026, 5, 30, 14, 0, tzinfo=timezone.utc),
    )
    session.add(row)
    session.flush()
    return row


def _drift(symbol: str, mysql_sum: float, broker_qty: float) -> Anomaly:
    return Anomaly(
        direction="qty_drift",
        symbol=symbol,
        strategy_id=None,
        snapshot={"mysql_sum": mysql_sum, "broker_qty": broker_qty},
    )


# ---------------------------------------------------------------------------
# 1. Strike gating
# ---------------------------------------------------------------------------


def test_below_threshold_does_not_close(store):
    cfg = _cfg(threshold=3)
    now = datetime(2026, 5, 31, 14, 0, tzinfo=timezone.utc)
    alpaca = MagicMock()
    sid = _strategy_id(store, "vwap_wave")

    with Session(store._engine) as session:
        _seed_strike(session, "AAPL", count=2, now=now)
        _seed_open_position(session, strategy_id=sid, symbol="AAPL",
                            setup="price_discovery", qty=100, asset_class="us_equity")
        session.commit()
        submitted = auto_resolve_qty_drift(
            alpaca=alpaca, store=store, session=session,
            broker_positions={"AAPL": {"symbol": "AAPL", "qty": "80",
                                        "current_price": "150",
                                        "asset_class": "us_equity"}},
            anomalies=[_drift("AAPL", 100, 80)],
            recent_fills=[], cfg=cfg, now=now,
        )
        session.commit()

    assert submitted == 0
    alpaca.submit_order.assert_not_called()


# ---------------------------------------------------------------------------
# 2-3. Single strategy, both directions
# ---------------------------------------------------------------------------


def test_broker_below_mysql_full_close_and_trim(store):
    """broker=80, MySQL=100 → submit full close at 80 and close MySQL row."""
    cfg = _cfg(threshold=3)
    now = datetime(2026, 5, 31, 14, 0, tzinfo=timezone.utc)
    alpaca = MagicMock()
    alpaca.list_orders.return_value = []
    alpaca.submit_order.return_value = {"id": "ord-1"}
    sid = _strategy_id(store, "vwap_wave")

    with Session(store._engine) as session:
        _seed_strike(session, "AAPL", count=3, now=now)
        _seed_open_position(session, strategy_id=sid, symbol="AAPL",
                            setup="price_discovery", qty=100,
                            asset_class="us_equity")
        session.commit()
        submitted = auto_resolve_qty_drift(
            alpaca=alpaca, store=store, session=session,
            broker_positions={"AAPL": {"symbol": "AAPL", "qty": "80",
                                        "current_price": "150",
                                        "asset_class": "us_equity"}},
            anomalies=[_drift("AAPL", 100, 80)],
            recent_fills=[], cfg=cfg, now=now,
        )
        session.commit()

    assert submitted == 1
    kwargs = alpaca.submit_order.call_args.kwargs
    assert kwargs["symbol"] == "AAPL"
    assert kwargs["qty"] == 80      # equity → no fee margin
    assert kwargs["side"] == "sell"

    with Session(store._engine) as session:
        row = session.query(PositionRow).one()
        assert row.status == "closed"
        assert row.close_reason == "auto_resolved_qty_drift"
        strike = session.query(StrikeRow).one()
        assert strike.resolved is True
        assert strike.resolved_reason == "auto_resolved_qty_drift"


def test_broker_above_mysql_closes_only_surplus(store):
    """broker=100, MySQL=80 → close 20 surplus, leave MySQL row open."""
    cfg = _cfg(threshold=3)
    now = datetime(2026, 5, 31, 14, 0, tzinfo=timezone.utc)
    alpaca = MagicMock()
    alpaca.list_orders.return_value = []
    alpaca.submit_order.return_value = {"id": "ord-2"}
    sid = _strategy_id(store, "vwap_wave")

    with Session(store._engine) as session:
        _seed_strike(session, "AAPL", count=3, now=now,
                     snapshot={"mysql_sum": 80.0, "broker_qty": 100.0})
        _seed_open_position(session, strategy_id=sid, symbol="AAPL",
                            setup="price_discovery", qty=80,
                            asset_class="us_equity")
        session.commit()
        auto_resolve_qty_drift(
            alpaca=alpaca, store=store, session=session,
            broker_positions={"AAPL": {"symbol": "AAPL", "qty": "100",
                                        "current_price": "150",
                                        "asset_class": "us_equity"}},
            anomalies=[_drift("AAPL", 80, 100)],
            recent_fills=[], cfg=cfg, now=now,
        )
        session.commit()

    kwargs = alpaca.submit_order.call_args.kwargs
    assert kwargs["qty"] == 20       # surplus only
    assert kwargs["side"] == "sell"  # broker is long, surplus closes by selling

    with Session(store._engine) as session:
        row = session.query(PositionRow).one()
        assert row.status == "open"   # MySQL row untouched
        strike = session.query(StrikeRow).one()
        assert strike.resolved is True


# ---------------------------------------------------------------------------
# 4-6. Multi-strategy attribution edge cases
# ---------------------------------------------------------------------------


def test_multi_strategy_unique_fill_match_picks_that_row(store):
    cfg = _cfg(threshold=3)
    now = datetime(2026, 5, 31, 14, 0, tzinfo=timezone.utc)
    alpaca = MagicMock()
    alpaca.list_orders.return_value = []
    alpaca.submit_order.return_value = {"id": "ord-3"}
    sid_a = _strategy_id(store, "vwap_wave")
    sid_b = _strategy_id(store, "orb")

    with Session(store._engine) as session:
        _seed_strike(session, "AAPL", count=3, now=now,
                     snapshot={"mysql_sum": 150.0, "broker_qty": 100.0})
        _seed_open_position(session, strategy_id=sid_a, symbol="AAPL",
                            setup="price_discovery", qty=100,
                            asset_class="us_equity")
        _seed_open_position(session, strategy_id=sid_b, symbol="AAPL",
                            setup="orb_breakout", qty=50,
                            asset_class="us_equity")
        session.commit()
        # Recent fill: vwap_wave's price_discovery setup partially exited.
        exit_coid = make_client_order_id(
            "vwap_wave", "price_discovery", "AAPL", Role.EXIT,
        )
        recent_fills = [
            {"symbol": "AAPL", "client_order_id": exit_coid, "id": "fill-1"},
        ]
        auto_resolve_qty_drift(
            alpaca=alpaca, store=store, session=session,
            broker_positions={"AAPL": {"symbol": "AAPL", "qty": "100",
                                        "current_price": "150",
                                        "asset_class": "us_equity"}},
            anomalies=[_drift("AAPL", 150, 100)],
            recent_fills=recent_fills, cfg=cfg, now=now,
        )
        session.commit()

    with Session(store._engine) as session:
        # Only vwap_wave's row got closed. orb's row stays open.
        rows = session.query(PositionRow).all()
        by_strategy = {r.strategy_id: r for r in rows}
        assert by_strategy[sid_a].status == "closed"
        assert by_strategy[sid_b].status == "open"


def test_multi_strategy_no_matching_fill_emits_ambiguous(store):
    cfg = _cfg(threshold=3)
    now = datetime(2026, 5, 31, 14, 0, tzinfo=timezone.utc)
    alpaca = MagicMock()
    sid_a = _strategy_id(store, "vwap_wave")
    sid_b = _strategy_id(store, "orb")

    with Session(store._engine) as session:
        _seed_strike(session, "AAPL", count=3, now=now,
                     snapshot={"mysql_sum": 150.0, "broker_qty": 100.0})
        _seed_open_position(session, strategy_id=sid_a, symbol="AAPL",
                            setup="price_discovery", qty=100,
                            asset_class="us_equity")
        _seed_open_position(session, strategy_id=sid_b, symbol="AAPL",
                            setup="orb_breakout", qty=50,
                            asset_class="us_equity")
        session.commit()
        auto_resolve_qty_drift(
            alpaca=alpaca, store=store, session=session,
            broker_positions={"AAPL": {"symbol": "AAPL", "qty": "100",
                                        "current_price": "150",
                                        "asset_class": "us_equity"}},
            anomalies=[_drift("AAPL", 150, 100)],
            recent_fills=[], cfg=cfg, now=now,
        )
        session.commit()

    alpaca.submit_order.assert_not_called()
    with Session(store._engine) as session:
        evts = session.query(EventRow).filter(
            EventRow.type == "qty_drift_ambiguous_attribution",
        ).all()
        assert len(evts) == 1
        strike = session.query(StrikeRow).one()
        assert strike.resolved is False
        rows = session.query(PositionRow).all()
        assert all(r.status == "open" for r in rows)


def test_multi_strategy_multiple_matching_fills_emits_ambiguous(store):
    cfg = _cfg(threshold=3)
    now = datetime(2026, 5, 31, 14, 0, tzinfo=timezone.utc)
    alpaca = MagicMock()
    sid_a = _strategy_id(store, "vwap_wave")
    sid_b = _strategy_id(store, "orb")

    with Session(store._engine) as session:
        _seed_strike(session, "AAPL", count=3, now=now,
                     snapshot={"mysql_sum": 150.0, "broker_qty": 100.0})
        _seed_open_position(session, strategy_id=sid_a, symbol="AAPL",
                            setup="price_discovery", qty=100,
                            asset_class="us_equity")
        _seed_open_position(session, strategy_id=sid_b, symbol="AAPL",
                            setup="orb_breakout", qty=50,
                            asset_class="us_equity")
        session.commit()
        recent_fills = [
            {"symbol": "AAPL",
             "client_order_id": make_client_order_id(
                 "vwap_wave", "price_discovery", "AAPL", Role.EXIT,
             ),
             "id": "fill-A"},
            {"symbol": "AAPL",
             "client_order_id": make_client_order_id(
                 "orb", "orb_breakout", "AAPL", Role.EXIT,
             ),
             "id": "fill-B"},
        ]
        auto_resolve_qty_drift(
            alpaca=alpaca, store=store, session=session,
            broker_positions={"AAPL": {"symbol": "AAPL", "qty": "100",
                                        "current_price": "150",
                                        "asset_class": "us_equity"}},
            anomalies=[_drift("AAPL", 150, 100)],
            recent_fills=recent_fills, cfg=cfg, now=now,
        )
        session.commit()

    alpaca.submit_order.assert_not_called()
    with Session(store._engine) as session:
        evts = session.query(EventRow).filter(
            EventRow.type == "qty_drift_ambiguous_attribution",
        ).all()
        assert len(evts) == 1


# ---------------------------------------------------------------------------
# 6b. Unattributable surplus — flatten broker excess, leave MySQL alone
# ---------------------------------------------------------------------------


def test_unattributed_surplus_closes_only_excess_and_leaves_mysql(store):
    """COIN-style scenario: broker holds far more than every open MySQL row
    combined could justify, COID attribution is ambiguous → close only the
    surplus, leave MySQL rows untouched, resolve the strike.
    """
    cfg = _cfg(threshold=3)
    now = datetime(2026, 5, 31, 14, 0, tzinfo=timezone.utc)
    alpaca = MagicMock()
    alpaca.list_orders.return_value = []
    alpaca.submit_order.return_value = {"id": "ord-surplus"}
    sid_a = _strategy_id(store, "vwap_wave")
    sid_b = _strategy_id(store, "orb")

    with Session(store._engine) as session:
        _seed_strike(session, "COIN", count=3, now=now,
                     snapshot={"mysql_sum": 1.0, "broker_qty": 22.0})
        # Two open rows, |qty| sum = 1; broker has 22 → surplus 21.
        _seed_open_position(session, strategy_id=sid_a, symbol="COIN",
                            setup="price_discovery", qty=1,
                            asset_class="us_equity")
        _seed_open_position(session, strategy_id=sid_b, symbol="COIN",
                            setup="orb_breakout", qty=-1, side="short",
                            asset_class="us_equity")
        session.commit()
        submitted = auto_resolve_qty_drift(
            alpaca=alpaca, store=store, session=session,
            broker_positions={"COIN": {"symbol": "COIN", "qty": "22",
                                        "current_price": "150",
                                        "asset_class": "us_equity"}},
            anomalies=[_drift("COIN", 0.0, 22.0)],
            recent_fills=[], cfg=cfg, now=now,
        )
        session.commit()

    assert submitted == 1
    kwargs = alpaca.submit_order.call_args.kwargs
    assert kwargs["symbol"] == "COIN"
    assert kwargs["side"] == "sell"
    assert kwargs["qty"] == 20.0  # surplus = 22 - (1 + 1) = 20

    with Session(store._engine) as session:
        # MySQL rows untouched.
        rows = session.query(PositionRow).all()
        assert len(rows) == 2
        assert all(r.status == "open" for r in rows)
        # Strike resolved with the new reason.
        strike = session.query(StrikeRow).one()
        assert strike.resolved is True
        assert strike.resolved_reason == "auto_close_qty_drift_surplus"
        # Audit event emitted.
        evts = session.query(EventRow).filter(
            EventRow.type == "auto_close_qty_drift_surplus",
        ).all()
        assert len(evts) == 1


def test_unattributed_no_surplus_still_emits_ambiguous(store):
    """When attribution is ambiguous but broker_qty <= sum(|mysql qty|),
    the surplus path must NOT fire — original ambiguous event must still
    be emitted so an operator can untangle.
    """
    cfg = _cfg(threshold=3)
    now = datetime(2026, 5, 31, 14, 0, tzinfo=timezone.utc)
    alpaca = MagicMock()
    sid_a = _strategy_id(store, "vwap_wave")
    sid_b = _strategy_id(store, "orb")

    with Session(store._engine) as session:
        _seed_strike(session, "AAPL", count=3, now=now,
                     snapshot={"mysql_sum": 150.0, "broker_qty": 100.0})
        _seed_open_position(session, strategy_id=sid_a, symbol="AAPL",
                            setup="price_discovery", qty=100,
                            asset_class="us_equity")
        _seed_open_position(session, strategy_id=sid_b, symbol="AAPL",
                            setup="orb_breakout", qty=50,
                            asset_class="us_equity")
        session.commit()
        auto_resolve_qty_drift(
            alpaca=alpaca, store=store, session=session,
            broker_positions={"AAPL": {"symbol": "AAPL", "qty": "100",
                                        "current_price": "150",
                                        "asset_class": "us_equity"}},
            anomalies=[_drift("AAPL", 150, 100)],
            recent_fills=[], cfg=cfg, now=now,
        )
        session.commit()

    alpaca.submit_order.assert_not_called()
    with Session(store._engine) as session:
        ambig = session.query(EventRow).filter(
            EventRow.type == "qty_drift_ambiguous_attribution",
        ).all()
        assert len(ambig) == 1
        surplus = session.query(EventRow).filter(
            EventRow.type == "auto_close_qty_drift_surplus",
        ).all()
        assert len(surplus) == 0
        strike = session.query(StrikeRow).one()
        assert strike.resolved is False


# ---------------------------------------------------------------------------
# 7-8. Asset class
# ---------------------------------------------------------------------------


def test_equity_close_qty_no_fee_margin(store):
    cfg = _cfg(threshold=3)
    now = datetime(2026, 5, 31, 14, 0, tzinfo=timezone.utc)
    alpaca = MagicMock()
    alpaca.list_orders.return_value = []
    alpaca.submit_order.return_value = {"id": "ord-eq"}
    sid = _strategy_id(store, "vwap_wave")

    with Session(store._engine) as session:
        _seed_strike(session, "AAPL", count=3, now=now)
        _seed_open_position(session, strategy_id=sid, symbol="AAPL",
                            setup="price_discovery", qty=100,
                            asset_class="us_equity")
        session.commit()
        auto_resolve_qty_drift(
            alpaca=alpaca, store=store, session=session,
            broker_positions={"AAPL": {"symbol": "AAPL", "qty": "80",
                                        "current_price": "150",
                                        "asset_class": "us_equity"}},
            anomalies=[_drift("AAPL", 100, 80)],
            recent_fills=[], cfg=cfg, now=now,
        )
        session.commit()

    assert alpaca.submit_order.call_args.kwargs["qty"] == 80


def test_crypto_close_qty_applies_fee_margin(store):
    cfg = _cfg(threshold=3)
    now = datetime(2026, 5, 31, 14, 0, tzinfo=timezone.utc)
    alpaca = MagicMock()
    alpaca.list_orders.return_value = []
    alpaca.submit_order.return_value = {"id": "ord-cr"}
    sid = _strategy_id(store, "vwap_wave")

    with Session(store._engine) as session:
        _seed_strike(session, "BTCUSD", count=3, now=now)
        _seed_open_position(session, strategy_id=sid, symbol="BTCUSD",
                            setup="price_discovery", qty=0.5,
                            asset_class="crypto")
        session.commit()
        auto_resolve_qty_drift(
            alpaca=alpaca, store=store, session=session,
            broker_positions={"BTCUSD": {"symbol": "BTC/USD", "qty": "0.4",
                                          "current_price": "60000",
                                          "asset_class": "crypto"}},
            anomalies=[_drift("BTCUSD", 0.5, 0.4)],
            recent_fills=[], cfg=cfg, now=now,
        )
        session.commit()

    submitted = alpaca.submit_order.call_args.kwargs["qty"]
    assert submitted == pytest.approx(0.4 * (1 - 1e-6), rel=1e-12)


# ---------------------------------------------------------------------------
# 9-10. Failure modes
# ---------------------------------------------------------------------------


def test_broker_submit_failure_keeps_strike_unresolved(store):
    cfg = _cfg(threshold=3)
    now = datetime(2026, 5, 31, 14, 0, tzinfo=timezone.utc)
    alpaca = MagicMock()
    alpaca.list_orders.return_value = []
    alpaca.submit_order.side_effect = RuntimeError("rate limited")
    sid = _strategy_id(store, "vwap_wave")

    with Session(store._engine) as session:
        _seed_strike(session, "AAPL", count=3, now=now)
        _seed_open_position(session, strategy_id=sid, symbol="AAPL",
                            setup="price_discovery", qty=100,
                            asset_class="us_equity")
        session.commit()
        auto_resolve_qty_drift(
            alpaca=alpaca, store=store, session=session,
            broker_positions={"AAPL": {"symbol": "AAPL", "qty": "80",
                                        "current_price": "150",
                                        "asset_class": "us_equity"}},
            anomalies=[_drift("AAPL", 100, 80)],
            recent_fills=[], cfg=cfg, now=now,
        )
        session.commit()

    with Session(store._engine) as session:
        evts = [e.type for e in session.query(EventRow).all()]
        assert "auto_close_qty_drift_failed" in evts
        strike = session.query(StrikeRow).one()
        assert strike.resolved is False
        # MySQL row not closed when submit failed.
        row = session.query(PositionRow).one()
        assert row.status == "open"


def test_shadow_mode_does_not_act(store):
    cfg = _cfg(shadow=True, threshold=3)
    now = datetime(2026, 5, 31, 14, 0, tzinfo=timezone.utc)
    alpaca = MagicMock()
    sid = _strategy_id(store, "vwap_wave")

    with Session(store._engine) as session:
        _seed_strike(session, "AAPL", count=3, now=now)
        _seed_open_position(session, strategy_id=sid, symbol="AAPL",
                            setup="price_discovery", qty=100,
                            asset_class="us_equity")
        session.commit()
        submitted = auto_resolve_qty_drift(
            alpaca=alpaca, store=store, session=session,
            broker_positions={"AAPL": {"symbol": "AAPL", "qty": "80",
                                        "current_price": "150",
                                        "asset_class": "us_equity"}},
            anomalies=[_drift("AAPL", 100, 80)],
            recent_fills=[], cfg=cfg, now=now,
        )
        session.commit()

    assert submitted == 0
    alpaca.submit_order.assert_not_called()
    with Session(store._engine) as session:
        row = session.query(PositionRow).one()
        assert row.status == "open"


# ---------------------------------------------------------------------------
# 11. Dust path resolves without submit
# ---------------------------------------------------------------------------


def test_dust_close_resolves_strike_without_submit(store):
    """A close_qty whose notional is below the dust threshold resolves
    the strike (no submit, since Alpaca would reject the tiny order)."""
    cfg = _cfg(threshold=3, dust_usd=10.0)
    now = datetime(2026, 5, 31, 14, 0, tzinfo=timezone.utc)
    alpaca = MagicMock()
    sid = _strategy_id(store, "vwap_wave")

    # 100 - 99.95 = 0.05 surplus, price 150 → 0.05 * 150 = $7.5 < $10 dust.
    with Session(store._engine) as session:
        _seed_strike(session, "AAPL", count=3, now=now,
                     snapshot={"mysql_sum": 99.95, "broker_qty": 100.0})
        _seed_open_position(session, strategy_id=sid, symbol="AAPL",
                            setup="price_discovery", qty=99.95,
                            asset_class="us_equity")
        session.commit()
        auto_resolve_qty_drift(
            alpaca=alpaca, store=store, session=session,
            broker_positions={"AAPL": {"symbol": "AAPL", "qty": "100",
                                        "current_price": "150",
                                        "asset_class": "us_equity"}},
            anomalies=[_drift("AAPL", 99.95, 100)],
            recent_fills=[], cfg=cfg, now=now,
        )
        session.commit()

    alpaca.submit_order.assert_not_called()
    with Session(store._engine) as session:
        strike = session.query(StrikeRow).one()
        assert strike.resolved is True
        assert strike.resolved_reason == "auto_close_dust"
