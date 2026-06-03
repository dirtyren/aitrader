"""Audit script detects MySQL/broker drift across both Alpaca accounts.

Detection rule: aggregate MySQL open rows by (symbol, asset_class) into a
signed qty (long=+, short=-). Compare to the broker's signed qty. Drift
when they differ. This handles multi-setup-per-symbol on the same account
(broker aggregates per symbol, MySQL has one row per setup).
"""
from __future__ import annotations

from scripts.audit_phantom_close_stacks import (
    aggregate_mysql_signed_qty,
    broker_signed_qty,
    detect_drift,
    format_report,
    DriftRow,
)


def test_aggregate_mysql_signed_qty_sums_setups():
    rows = [
        {"symbol": "COIN", "asset_class": "equity",
         "side": "short", "qty": 1.0, "setup_name": "price_discovery",
         "id": 1, "opened_at": "2026-06-02T15:00:00Z",
         "client_order_id": "coid-1"},
        {"symbol": "COIN", "asset_class": "equity",
         "side": "short", "qty": 2.0, "setup_name": "fade_extreme",
         "id": 2, "opened_at": "2026-06-02T15:30:00Z",
         "client_order_id": "coid-2"},
    ]
    result = aggregate_mysql_signed_qty(rows)
    assert result[("COIN", "equity")]["signed_qty"] == -3.0
    assert len(result[("COIN", "equity")]["rows"]) == 2


def test_aggregate_mysql_signed_qty_long_short_offset():
    rows = [
        {"symbol": "COIN", "asset_class": "equity",
         "side": "long", "qty": 5.0, "setup_name": "a",
         "id": 1, "opened_at": "2026-06-02T15:00:00Z", "client_order_id": ""},
        {"symbol": "COIN", "asset_class": "equity",
         "side": "short", "qty": 2.0, "setup_name": "b",
         "id": 2, "opened_at": "2026-06-02T15:30:00Z", "client_order_id": ""},
    ]
    result = aggregate_mysql_signed_qty(rows)
    assert result[("COIN", "equity")]["signed_qty"] == 3.0


def test_broker_signed_qty_long():
    pos = {"symbol": "COIN", "qty": "22", "side": "long",
           "asset_class": "us_equity", "avg_entry_price": "174.30"}
    assert broker_signed_qty(pos) == 22.0


def test_broker_signed_qty_short():
    pos = {"symbol": "COIN", "qty": "-22", "side": "short",
           "asset_class": "us_equity", "avg_entry_price": "174.30"}
    assert broker_signed_qty(pos) == -22.0


def test_detect_drift_today_coin_incident():
    """Today's actual incident: MySQL has 1 short, broker has 22 long."""
    mysql_rows = [{
        "symbol": "COIN", "asset_class": "equity",
        "side": "short", "qty": 1.0, "setup_name": "price_discovery",
        "id": 1234, "opened_at": "2026-06-02T15:00:00Z",
        "client_order_id": "coid-stuck",
    }]
    broker_positions = [{
        "symbol": "COIN", "qty": "22", "side": "long",
        "asset_class": "us_equity", "avg_entry_price": "174.30",
    }]
    drifts = detect_drift(mysql_rows, broker_positions)
    assert len(drifts) == 1
    d = drifts[0]
    assert d.symbol == "COIN"
    assert d.asset_class == "equity"
    assert d.mysql_signed_qty == -1.0
    assert d.broker_signed_qty == 22.0
    assert d.delta == 23.0
    assert d.suggested_flatten_side == "sell"
    assert d.suggested_flatten_qty == 23.0  # close the entire delta


def test_detect_drift_no_drift_returns_empty():
    mysql_rows = [{
        "symbol": "COIN", "asset_class": "equity",
        "side": "short", "qty": 1.0, "setup_name": "price_discovery",
        "id": 1, "opened_at": "2026-06-02T15:00:00Z",
        "client_order_id": "ok",
    }]
    broker_positions = [{
        "symbol": "COIN", "qty": "-1", "side": "short",
        "asset_class": "us_equity", "avg_entry_price": "174.07",
    }]
    drifts = detect_drift(mysql_rows, broker_positions)
    assert drifts == []


def test_detect_drift_mysql_only_no_broker_position():
    """MySQL has an open row but the broker shows no position. Drift."""
    mysql_rows = [{
        "symbol": "COIN", "asset_class": "equity",
        "side": "short", "qty": 1.0, "setup_name": "price_discovery",
        "id": 1, "opened_at": "2026-06-02T15:00:00Z",
        "client_order_id": "ok",
    }]
    drifts = detect_drift(mysql_rows, broker_positions=[])
    assert len(drifts) == 1
    assert drifts[0].broker_signed_qty == 0.0
    assert drifts[0].mysql_signed_qty == -1.0


def test_detect_drift_broker_only_no_mysql_row():
    drifts = detect_drift(
        mysql_rows=[],
        broker_positions=[{
            "symbol": "GHOST", "qty": "5", "side": "long",
            "asset_class": "us_equity", "avg_entry_price": "10.00",
        }],
    )
    assert len(drifts) == 1
    assert drifts[0].mysql_signed_qty == 0.0
    assert drifts[0].broker_signed_qty == 5.0


def test_format_report_includes_suggested_flatten():
    drift = DriftRow(
        symbol="COIN", asset_class="equity",
        mysql_signed_qty=-1.0, broker_signed_qty=22.0, delta=23.0,
        mysql_rows=[{
            "id": 1234, "setup_name": "price_discovery",
            "side": "short", "qty": 1.0,
            "opened_at": "2026-06-02T15:00:00Z",
            "client_order_id": "coid-stuck",
        }],
        broker_position={
            "symbol": "COIN", "qty": "22", "side": "long",
            "avg_entry_price": "174.30",
        },
        suggested_flatten_side="sell",
        suggested_flatten_qty=23.0,
    )
    out = format_report([drift])
    assert "DRIFT symbol=COIN" in out
    assert "id=1234" in out and "price_discovery" in out
    assert "qty=22" in out
    assert "suggested_manual_flatten" in out
    assert "side=sell" in out and "qty=23" in out


def test_format_report_no_drift_says_so():
    out = format_report([])
    assert "no drift" in out.lower()
