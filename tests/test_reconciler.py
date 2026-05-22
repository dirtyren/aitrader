import pytest
from state.reconciler import (
    _normalize_asset_class,
    _normalize_side,
    _index_bracket_children,
)


def test_normalize_asset_class_us_equity():
    assert _normalize_asset_class("us_equity") == "equity"


def test_normalize_asset_class_crypto():
    assert _normalize_asset_class("crypto") == "crypto"


def test_normalize_asset_class_uppercase():
    assert _normalize_asset_class("US_EQUITY") == "equity"


def test_normalize_asset_class_unknown_returns_none():
    assert _normalize_asset_class("forex") is None
    assert _normalize_asset_class("") is None
    assert _normalize_asset_class(None) is None


def test_normalize_side_long():
    assert _normalize_side("long") == "long"


def test_normalize_side_short():
    assert _normalize_side("short") == "short"


def test_normalize_side_uppercase():
    assert _normalize_side("LONG") == "long"


def test_normalize_side_unknown_raises():
    with pytest.raises(ValueError):
        _normalize_side("buy")


def test_index_bracket_children_nested_legs():
    parent = {
        "id": "p1", "symbol": "AAPL", "type": "limit", "side": "buy",
        "legs": [
            {"id": "stop1", "symbol": "AAPL", "type": "stop",
             "stop_price": "99.0", "side": "sell"},
            {"id": "tgt1", "symbol": "AAPL", "type": "limit",
             "limit_price": "102.0", "side": "sell"},
        ],
    }
    idx = _index_bracket_children([parent])
    assert idx["AAPL"]["stop"]["id"] == "stop1"
    assert idx["AAPL"]["target"]["id"] == "tgt1"


def test_index_bracket_children_orphaned_children():
    children = [
        {"id": "stop1", "symbol": "AAPL", "type": "stop_limit",
         "stop_price": "99.0", "parent_id": "p1", "side": "sell"},
        {"id": "tgt1", "symbol": "AAPL", "type": "limit",
         "limit_price": "102.0", "parent_id": "p1", "side": "sell"},
    ]
    idx = _index_bracket_children(children)
    assert idx["AAPL"]["stop"]["id"] == "stop1"
    assert idx["AAPL"]["target"]["id"] == "tgt1"


def test_index_bracket_children_only_stop_present():
    orders = [
        {"id": "stop1", "symbol": "AAPL", "type": "stop",
         "stop_price": "99.0", "side": "sell"},
    ]
    idx = _index_bracket_children(orders)
    assert idx["AAPL"]["stop"]["id"] == "stop1"
    assert idx["AAPL"]["target"] is None


def test_index_bracket_children_empty_input():
    assert _index_bracket_children([]) == {}


def test_index_bracket_children_ignores_unrelated_order_types():
    orders = [
        {"id": "m1", "symbol": "AAPL", "type": "market", "side": "buy"},
    ]
    assert _index_bracket_children(orders) == {}
