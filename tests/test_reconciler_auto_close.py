"""Auto-close path for broker_only orphans confirmed by the strike rule.

The reconciler walks broker positions every cycle. Any symbol the broker
reports as open with no corresponding open MySQL row is a broker_only
anomaly. After `cfg.strike_threshold` consecutive observations spaced
`>= cfg.strike_min_gap_s` apart, the position is confirmed unmanaged and
must be auto-closed — aitrader cannot enforce stops/targets on positions
it doesn't track.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from reconciler.config import ReconcilerConfig
from reconciler.main import auto_close_broker_only, run_one_cycle
from reconciler.invariant import Anomaly
from state.mysql_store import (
    Base,
    EventRow,
    MySQLStore,
    StrategyRow,
    StrikeRow,
)


@pytest.fixture
def store():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    s = MySQLStore.__new__(MySQLStore)
    s._engine = engine
    s.strategy_name = "reconciler"
    s._log = logging.getLogger("test_auto_close")
    with Session(engine) as session:
        session.add(StrategyRow(name="vwap_wave"))
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
    )


def _seed_strike(session, symbol: str, count: int, *, now: datetime,
                 direction: str = "broker_only") -> StrikeRow:
    """Pre-seed a strike at the given count (simulates prior cycles)."""
    row = StrikeRow(
        key=f"{direction}:{symbol}",
        direction=direction,
        strategy_id=None,
        symbol=symbol,
        strike_count=count,
        first_seen_at=now - timedelta(minutes=count * 2),
        last_seen_at=now - timedelta(seconds=120),
        last_observed_state={"mysql_sum": 0.0, "broker_qty": 1.0},
        resolved=False,
    )
    session.add(row)
    session.flush()
    return row


def _broker_only_anomaly(symbol="SOLUSD", broker_qty=1233.0):
    return Anomaly(
        direction="broker_only",
        symbol=symbol,
        strategy_id=None,
        snapshot={"mysql_sum": 0.0, "broker_qty": broker_qty},
    )


# ---------------------------------------------------------------------------
# auto_close_broker_only — direct unit tests
# ---------------------------------------------------------------------------


def test_below_threshold_does_not_close(store):
    """A strike at count=1 must NOT trigger an auto-close — wait for
    confirmation across multiple cycles to filter transient races."""
    cfg = _cfg(threshold=3)
    now = datetime(2026, 5, 29, 14, 0, tzinfo=timezone.utc)
    alpaca = MagicMock()

    with Session(store._engine) as session:
        _seed_strike(session, "SOLUSD", count=1, now=now)
        session.commit()
        anomaly = _broker_only_anomaly("SOLUSD")
        submitted = auto_close_broker_only(
            alpaca=alpaca, store=store, session=session,
            broker_positions={"SOLUSD": {"symbol": "SOL/USD", "qty": "1233.0", "current_price": "50.0"}},
            anomalies=[anomaly], cfg=cfg, now=now,
        )
        session.commit()

    assert submitted == 0
    alpaca.submit_order.assert_not_called()


def test_at_threshold_market_closes_long_position(store):
    cfg = _cfg(threshold=3)
    now = datetime(2026, 5, 29, 14, 0, tzinfo=timezone.utc)
    alpaca = MagicMock()
    alpaca.submit_order.return_value = {"id": "close-1"}

    with Session(store._engine) as session:
        _seed_strike(session, "SOLUSD", count=3, now=now)
        session.commit()
        anomaly = _broker_only_anomaly("SOLUSD", broker_qty=1233.0)
        submitted = auto_close_broker_only(
            alpaca=alpaca, store=store, session=session,
            broker_positions={"SOLUSD": {"symbol": "SOL/USD", "qty": "1233.0", "current_price": "50.0"}},
            anomalies=[anomaly], cfg=cfg, now=now,
        )
        session.commit()

    assert submitted == 1
    alpaca.submit_order.assert_called_once()
    kwargs = alpaca.submit_order.call_args.kwargs
    assert kwargs["symbol"] == "SOL/USD"        # broker's own slash form preserved
    assert kwargs["qty"] == 1233.0
    assert kwargs["side"] == "sell"             # close a long
    assert kwargs["order_type"] == "market"

    with Session(store._engine) as session:
        row = session.query(StrikeRow).filter(StrikeRow.symbol == "SOLUSD").one()
        assert row.resolved is True
        assert row.resolved_reason == "auto_closed_broker_only"


def test_short_position_closes_with_buy(store):
    cfg = _cfg(threshold=3)
    now = datetime(2026, 5, 29, 14, 0, tzinfo=timezone.utc)
    alpaca = MagicMock()
    alpaca.submit_order.return_value = {"id": "close-2"}

    with Session(store._engine) as session:
        _seed_strike(session, "PLTR", count=3, now=now)
        session.commit()
        anomaly = _broker_only_anomaly("PLTR", broker_qty=-50.0)
        auto_close_broker_only(
            alpaca=alpaca, store=store, session=session,
            broker_positions={"PLTR": {"symbol": "PLTR", "qty": "-50",
                                       "current_price": "100.0"}},
            anomalies=[anomaly], cfg=cfg, now=now,
        )
        session.commit()

    kwargs = alpaca.submit_order.call_args.kwargs
    assert kwargs["side"] == "buy"
    assert kwargs["qty"] == 50.0


def test_shadow_mode_does_not_close(store):
    """Shadow mode is the audit-only mode — it must never mutate the broker."""
    cfg = _cfg(shadow=True, threshold=3)
    now = datetime(2026, 5, 29, 14, 0, tzinfo=timezone.utc)
    alpaca = MagicMock()

    with Session(store._engine) as session:
        _seed_strike(session, "SOLUSD", count=3, now=now)
        session.commit()
        submitted = auto_close_broker_only(
            alpaca=alpaca, store=store, session=session,
            broker_positions={"SOLUSD": {"symbol": "SOL/USD", "qty": "1233.0", "current_price": "50.0"}},
            anomalies=[_broker_only_anomaly("SOLUSD")], cfg=cfg, now=now,
        )

    assert submitted == 0
    alpaca.submit_order.assert_not_called()


def test_close_failure_does_not_resolve_strike(store):
    """A failed market close must leave the strike unresolved so the next
    cycle retries. A failure event is emitted for visibility."""
    cfg = _cfg(threshold=3)
    now = datetime(2026, 5, 29, 14, 0, tzinfo=timezone.utc)
    alpaca = MagicMock()
    alpaca.submit_order.side_effect = RuntimeError("rate limited")

    with Session(store._engine) as session:
        _seed_strike(session, "SOLUSD", count=3, now=now)
        session.commit()
        submitted = auto_close_broker_only(
            alpaca=alpaca, store=store, session=session,
            broker_positions={"SOLUSD": {"symbol": "SOL/USD", "qty": "1233.0", "current_price": "50.0"}},
            anomalies=[_broker_only_anomaly("SOLUSD")], cfg=cfg, now=now,
        )
        session.commit()

    assert submitted == 0  # exception path returns submitted unchanged

    with Session(store._engine) as session:
        row = session.query(StrikeRow).filter(StrikeRow.symbol == "SOLUSD").one()
        assert row.resolved is False  # still pending → next cycle retries
        types = [e.type for e in session.query(EventRow).all()]
        assert "auto_close_broker_only_failed" in types


def test_emits_event_on_successful_close(store):
    cfg = _cfg(threshold=3)
    now = datetime(2026, 5, 29, 14, 0, tzinfo=timezone.utc)
    alpaca = MagicMock()
    alpaca.submit_order.return_value = {"id": "close-evt"}

    with Session(store._engine) as session:
        _seed_strike(session, "SOLUSD", count=3, now=now)
        session.commit()
        auto_close_broker_only(
            alpaca=alpaca, store=store, session=session,
            broker_positions={"SOLUSD": {"symbol": "SOL/USD", "qty": "1233.0", "current_price": "50.0"}},
            anomalies=[_broker_only_anomaly("SOLUSD")], cfg=cfg, now=now,
        )
        session.commit()

    with Session(store._engine) as session:
        evts = session.query(EventRow).filter(
            EventRow.type == "auto_close_broker_only",
        ).all()
        assert len(evts) == 1
        payload = evts[0].payload
        assert payload["broker_symbol"] == "SOL/USD"
        assert payload["side"] == "sell"
        assert payload["total_qty"] == 1233.0
        assert payload["chunks"] == 1
        assert payload["order_ids"] == ["close-evt"]
        assert payload["strike_count"] == 3


def test_position_vanished_between_snapshot_and_close_is_skipped(store):
    """Race: anomaly detected, strike at threshold, but the position is no
    longer in broker_positions (closed externally between cycles). Skip
    cleanly; auto_clear_resolved will tidy the strike next cycle."""
    cfg = _cfg(threshold=3)
    now = datetime(2026, 5, 29, 14, 0, tzinfo=timezone.utc)
    alpaca = MagicMock()

    with Session(store._engine) as session:
        _seed_strike(session, "GHOST", count=3, now=now)
        session.commit()
        submitted = auto_close_broker_only(
            alpaca=alpaca, store=store, session=session,
            broker_positions={},  # symbol gone
            anomalies=[_broker_only_anomaly("GHOST")], cfg=cfg, now=now,
        )

    assert submitted == 0
    alpaca.submit_order.assert_not_called()


def test_zero_qty_position_is_skipped(store):
    """Defense against malformed broker payloads — qty=0 means there's
    nothing to close, even though the symbol appeared in the snapshot."""
    cfg = _cfg(threshold=3)
    now = datetime(2026, 5, 29, 14, 0, tzinfo=timezone.utc)
    alpaca = MagicMock()

    with Session(store._engine) as session:
        _seed_strike(session, "WEIRD", count=3, now=now)
        session.commit()
        submitted = auto_close_broker_only(
            alpaca=alpaca, store=store, session=session,
            broker_positions={"WEIRD": {"symbol": "WEIRD", "qty": "0"}},
            anomalies=[_broker_only_anomaly("WEIRD", broker_qty=0.0)],
            cfg=cfg, now=now,
        )

    assert submitted == 0
    alpaca.submit_order.assert_not_called()


def test_only_broker_only_direction_is_auto_closed(store):
    """qty_drift / mysql_only anomalies must NOT route through auto-close —
    they have different remediation paths."""
    cfg = _cfg(threshold=3)
    now = datetime(2026, 5, 29, 14, 0, tzinfo=timezone.utc)
    alpaca = MagicMock()

    drift = Anomaly(
        direction="qty_drift", symbol="AAPL", strategy_id=None,
        snapshot={"mysql_sum": 10.0, "broker_qty": 12.0},
    )
    with Session(store._engine) as session:
        _seed_strike(session, "AAPL", count=3, now=now, direction="qty_drift")
        session.commit()
        submitted = auto_close_broker_only(
            alpaca=alpaca, store=store, session=session,
            broker_positions={"AAPL": {"symbol": "AAPL", "qty": "12",
                                       "current_price": "200.0"}},
            anomalies=[drift], cfg=cfg, now=now,
        )

    assert submitted == 0
    alpaca.submit_order.assert_not_called()


# ---------------------------------------------------------------------------
# End-to-end through run_one_cycle: 3 successive cycles → close on cycle 3
# ---------------------------------------------------------------------------


def test_three_cycle_progression_auto_closes_on_third(store):
    """Cycle 1 logs strike 1. Cycle 2 advances to 2. Cycle 3 reaches 3 and
    auto-closes. After the close fills (cycle 4 sees no broker position),
    the strike self-heals."""
    cfg = _cfg(threshold=3, min_gap=10)
    alpaca = MagicMock()
    alpaca.list_orders.return_value = []
    alpaca.submit_order.return_value = {"id": "close-e2e"}
    alpaca.get_positions.return_value = [
        {"symbol": "SOL/USD", "qty": "1233.0", "side": "long",
         "asset_class": "crypto", "current_price": "50.0"},
    ]

    base = datetime(2026, 5, 29, 14, 0, tzinfo=timezone.utc)
    for i in range(3):
        run_one_cycle(
            store=store, alpaca=alpaca, cfg=cfg,
            last_orders_check_ts=None, now=base + timedelta(seconds=30 * i),
        )

    # Auto-close fired exactly once — on the cycle the strike count reached 3.
    assert alpaca.submit_order.call_count == 1
    kwargs = alpaca.submit_order.call_args.kwargs
    assert kwargs["symbol"] == "SOL/USD"
    assert kwargs["qty"] == 1233.0
    assert kwargs["side"] == "sell"

    with Session(store._engine) as session:
        row = session.query(StrikeRow).filter(StrikeRow.symbol == "SOLUSD").one()
        assert row.resolved is True
        assert row.resolved_reason == "auto_closed_broker_only"
        assert row.strike_count == 3


# ---------------------------------------------------------------------------
# Notional-aware chunking — Alpaca rejects single orders > $200k notional
# ---------------------------------------------------------------------------


def _cfg_with_max(max_notional: float, threshold: int = 3):
    return ReconcilerConfig(
        interval_s=30, strike_threshold=threshold, strike_min_gap_s=60,
        qty_eps=1e-6, shadow_mode=False,
        state_file_path="/tmp/state.json",
        heartbeat_stale_after_s=300,
        auto_close_max_notional_usd=max_notional,
    )


def test_split_qty_helper_returns_full_chunks_plus_remainder():
    from reconciler.main import _split_qty
    chunks = _split_qty(total_qty=2300.0, max_qty_per_chunk=1000.0)
    assert chunks == [1000.0, 1000.0, 300.0]


def test_split_qty_helper_single_chunk_when_under_cap():
    from reconciler.main import _split_qty
    assert _split_qty(total_qty=500.0, max_qty_per_chunk=1000.0) == [500.0]


def test_split_qty_helper_no_remainder_when_exactly_divisible():
    from reconciler.main import _split_qty
    chunks = _split_qty(total_qty=3000.0, max_qty_per_chunk=1000.0)
    assert chunks == [1000.0, 1000.0, 1000.0]


def test_oversized_position_splits_into_two_chunks(store):
    """SOL/USD at $90 with 2296 qty → notional = $206k, exceeds $190k cap.
    Must split into two market orders, each under the cap."""
    cfg = _cfg_with_max(max_notional=190_000.0)
    now = datetime(2026, 5, 29, 14, 0, tzinfo=timezone.utc)
    alpaca = MagicMock()
    alpaca.submit_order.side_effect = [
        {"id": "close-chunk-1"},
        {"id": "close-chunk-2"},
    ]

    with Session(store._engine) as session:
        _seed_strike(session, "SOLUSD", count=3, now=now)
        session.commit()
        anomaly = _broker_only_anomaly("SOLUSD", broker_qty=2296.602350897)
        submitted = auto_close_broker_only(
            alpaca=alpaca, store=store, session=session,
            broker_positions={
                "SOLUSD": {"symbol": "SOL/USD", "qty": "2296.602350897",
                           "current_price": "90.0"},
            },
            anomalies=[anomaly], cfg=cfg, now=now,
        )
        session.commit()

    # 2296.602 * 90 = ~$206.7k → 1 full chunk @ qty 2111.111... ($190k)
    # plus a remainder chunk. Two submits total.
    assert submitted == 2
    assert alpaca.submit_order.call_count == 2
    # Each chunk must close the same direction; combined qty == total qty.
    qtys = [c.kwargs["qty"] for c in alpaca.submit_order.call_args_list]
    sides = [c.kwargs["side"] for c in alpaca.submit_order.call_args_list]
    assert all(s == "sell" for s in sides)
    assert sum(qtys) == pytest.approx(2296.602350897)
    # Each chunk must be sized so its notional is at-or-below the cap.
    for q in qtys:
        assert q * 90.0 <= 190_000.0 + 1e-6

    with Session(store._engine) as session:
        row = session.query(StrikeRow).filter(StrikeRow.symbol == "SOLUSD").one()
        assert row.resolved is True
        assert row.resolved_reason == "auto_closed_broker_only"


def test_chunked_close_emits_single_event_with_all_order_ids(store):
    cfg = _cfg_with_max(max_notional=190_000.0)
    now = datetime(2026, 5, 29, 14, 0, tzinfo=timezone.utc)
    alpaca = MagicMock()
    alpaca.submit_order.side_effect = [
        {"id": "ord-A"}, {"id": "ord-B"}, {"id": "ord-C"},
    ]

    with Session(store._engine) as session:
        _seed_strike(session, "BIG", count=3, now=now)
        session.commit()
        # qty * price = 5000 * 100 = $500k → 3 chunks
        anomaly = _broker_only_anomaly("BIG", broker_qty=5000.0)
        auto_close_broker_only(
            alpaca=alpaca, store=store, session=session,
            broker_positions={
                "BIG": {"symbol": "BIG", "qty": "5000",
                        "current_price": "100.0"},
            },
            anomalies=[anomaly], cfg=cfg, now=now,
        )
        session.commit()

    with Session(store._engine) as session:
        evts = session.query(EventRow).filter(
            EventRow.type == "auto_close_broker_only",
        ).all()
        assert len(evts) == 1
        payload = evts[0].payload
        assert payload["chunks"] == 3
        assert payload["order_ids"] == ["ord-A", "ord-B", "ord-C"]
        assert payload["total_qty"] == 5000.0


def test_chunked_close_partial_failure_leaves_strike_unresolved(store):
    """If chunk #2 fails after chunk #1 succeeded, the strike must NOT be
    marked resolved — next cycle sees the broker holding (total - chunk1)
    and retries from there."""
    cfg = _cfg_with_max(max_notional=190_000.0)
    now = datetime(2026, 5, 29, 14, 0, tzinfo=timezone.utc)
    alpaca = MagicMock()
    alpaca.submit_order.side_effect = [
        {"id": "ord-A"},
        RuntimeError("rate limited"),
    ]

    with Session(store._engine) as session:
        _seed_strike(session, "SOLUSD", count=3, now=now)
        session.commit()
        anomaly = _broker_only_anomaly("SOLUSD", broker_qty=2296.0)
        submitted = auto_close_broker_only(
            alpaca=alpaca, store=store, session=session,
            broker_positions={
                "SOLUSD": {"symbol": "SOL/USD", "qty": "2296.0",
                           "current_price": "90.0"},
            },
            anomalies=[anomaly], cfg=cfg, now=now,
        )
        session.commit()

    # Chunk 1 went through; chunk 2 raised. Submitted == 1.
    assert submitted == 1
    assert alpaca.submit_order.call_count == 2

    with Session(store._engine) as session:
        row = session.query(StrikeRow).filter(StrikeRow.symbol == "SOLUSD").one()
        assert row.resolved is False  # next cycle retries
        types = [e.type for e in session.query(EventRow).all()]
        assert "auto_close_broker_only_failed" in types
        # The success event must NOT be emitted on partial submission.
        assert "auto_close_broker_only" not in types


def test_position_without_price_field_is_skipped(store):
    """A broker payload missing both current_price and avg_entry_price
    cannot be safely chunked. Skip + emit a failed event."""
    cfg = _cfg_with_max(max_notional=190_000.0)
    now = datetime(2026, 5, 29, 14, 0, tzinfo=timezone.utc)
    alpaca = MagicMock()

    with Session(store._engine) as session:
        _seed_strike(session, "PRICELESS", count=3, now=now)
        session.commit()
        anomaly = _broker_only_anomaly("PRICELESS", broker_qty=10.0)
        submitted = auto_close_broker_only(
            alpaca=alpaca, store=store, session=session,
            broker_positions={
                "PRICELESS": {"symbol": "PRICELESS", "qty": "10"},  # no price
            },
            anomalies=[anomaly], cfg=cfg, now=now,
        )
        session.commit()

    assert submitted == 0
    alpaca.submit_order.assert_not_called()

    with Session(store._engine) as session:
        row = session.query(StrikeRow).filter(StrikeRow.symbol == "PRICELESS").one()
        assert row.resolved is False
        types = [e.type for e in session.query(EventRow).all()]
        assert "auto_close_broker_only_failed" in types


def test_avg_entry_price_used_when_current_price_missing(store):
    """Fallback to avg_entry_price when Alpaca's mark is missing."""
    cfg = _cfg_with_max(max_notional=190_000.0)
    now = datetime(2026, 5, 29, 14, 0, tzinfo=timezone.utc)
    alpaca = MagicMock()
    alpaca.submit_order.return_value = {"id": "close-via-entry"}

    with Session(store._engine) as session:
        _seed_strike(session, "SOLUSD", count=3, now=now)
        session.commit()
        auto_close_broker_only(
            alpaca=alpaca, store=store, session=session,
            broker_positions={
                "SOLUSD": {"symbol": "SOL/USD", "qty": "1000.0",
                           "avg_entry_price": "85.0"},
            },
            anomalies=[_broker_only_anomaly("SOLUSD")], cfg=cfg, now=now,
        )

    alpaca.submit_order.assert_called_once()


def test_too_many_chunks_aborts_before_any_submit(store):
    """Defensive backstop: a near-zero price would compute thousands of
    chunks. Refuse to submit any of them; surface a failed event."""
    cfg = _cfg_with_max(max_notional=190_000.0)
    now = datetime(2026, 5, 29, 14, 0, tzinfo=timezone.utc)
    alpaca = MagicMock()

    with Session(store._engine) as session:
        _seed_strike(session, "GLITCH", count=3, now=now)
        session.commit()
        # qty 1e13 at $0.0001 → notional $1B; max-qty-per-chunk = 1.9e9 →
        # ~526 chunks, well past the 50-chunk defensive backstop.
        anomaly = _broker_only_anomaly("GLITCH", broker_qty=1e13)
        submitted = auto_close_broker_only(
            alpaca=alpaca, store=store, session=session,
            broker_positions={
                "GLITCH": {"symbol": "GLITCH", "qty": "10000000000000",
                           "current_price": "0.0001"},
            },
            anomalies=[anomaly], cfg=cfg, now=now,
        )
        session.commit()

    assert submitted == 0
    alpaca.submit_order.assert_not_called()

    with Session(store._engine) as session:
        row = session.query(StrikeRow).filter(StrikeRow.symbol == "GLITCH").one()
        assert row.resolved is False
        evts = session.query(EventRow).filter(
            EventRow.type == "auto_close_broker_only_failed",
        ).all()
        assert len(evts) == 1
        assert "too many chunks" in evts[0].payload["error"]
