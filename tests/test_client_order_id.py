"""Tests for the client_order_id (COID) format helpers.

The COID format is the canonical attribution mechanism for orders submitted
to Alpaca. Every order's COID encodes (strategy, setup, symbol, role) so
fills can be matched back to the originating MySQL row by Plan 3's reconciler.
"""
from __future__ import annotations

import re

import pytest

from broker.client_order_id import (
    MAX_LENGTH,
    Role,
    make_client_order_id,
    parse_client_order_id,
)


# ── Roles ──────────────────────────────────────────────────────────────


def test_role_values_are_canonical():
    assert Role.ENTRY == "entry"
    assert Role.EXIT == "exit"
    assert Role.STOP == "stop"
    assert Role.TARGET == "target"
    assert Role.ADOPTED == "adopted"


def test_make_rejects_unknown_role():
    with pytest.raises(ValueError, match="role"):
        make_client_order_id("vwap_wave", "vwap_bounce", "BTCUSD", "rebalance")


# ── make_client_order_id ───────────────────────────────────────────────


def test_make_produces_expected_shape():
    coid = make_client_order_id("vwap_wave", "vwap_bounce", "BTCUSD", Role.ENTRY)
    parts = coid.split("__")
    assert parts[0] == "aitrader"
    assert parts[1] == "vwap_wave"
    assert parts[2] == "vwap_bounce"
    assert parts[3] == "BTCUSD"
    assert parts[4] == "entry"
    assert re.fullmatch(r"[0-9a-f]{8}", parts[5])


def test_make_under_max_length():
    # Worst-case-ish inputs; result must still fit within Alpaca's 128 cap.
    coid = make_client_order_id(
        "a_very_long_strategy_name_with_lots_of_chars_xxxxxx",
        "an_equally_long_setup_name_zzzzzzzzzzzzzzzzzzzzzzz",
        "VERYLONGSYMBOL12",
        Role.ENTRY,
    )
    assert len(coid) <= MAX_LENGTH


def test_make_lowercases_strategy_setup_role():
    coid = make_client_order_id("VWAP_Wave", "VWAP_Bounce", "btcusd", Role.ENTRY)
    parts = coid.split("__")
    assert parts[1] == "vwap_wave"
    assert parts[2] == "vwap_bounce"
    # Symbol is forced uppercase
    assert parts[3] == "BTCUSD"


def test_make_replaces_disallowed_chars_with_underscore():
    coid = make_client_order_id("vwap-wave!", "v.bounce", "AAPL", Role.ENTRY)
    parts = coid.split("__")
    # Trailing/leading underscores are stripped to prevent collisions with the __ separator;
    # "vwap-wave!" -> "vwap_wave_" -> stripped to "vwap_wave".
    assert parts[1] == "vwap_wave"
    assert parts[2] == "v_bounce"


def test_make_strips_slash_from_symbol():
    coid = make_client_order_id("vwap_wave", "vwap_bounce", "BTC/USD", Role.ENTRY)
    parts = coid.split("__")
    assert parts[3] == "BTCUSD"


def test_make_uniqueness_via_uuid_suffix():
    a = make_client_order_id("vwap_wave", "vwap_bounce", "AAPL", Role.ENTRY)
    b = make_client_order_id("vwap_wave", "vwap_bounce", "AAPL", Role.ENTRY)
    assert a != b
    assert a.split("__")[:5] == b.split("__")[:5]


def test_make_empty_inputs_rejected():
    with pytest.raises(ValueError):
        make_client_order_id("", "vwap_bounce", "AAPL", Role.ENTRY)
    with pytest.raises(ValueError):
        make_client_order_id("vwap_wave", "", "AAPL", Role.ENTRY)
    with pytest.raises(ValueError):
        make_client_order_id("vwap_wave", "vwap_bounce", "", Role.ENTRY)


# ── parse_client_order_id ─────────────────────────────────────────────


def test_parse_round_trip():
    coid = make_client_order_id("vwap_wave", "vwap_bounce", "BTCUSD", Role.EXIT)
    parsed = parse_client_order_id(coid)
    assert parsed == {
        "strategy": "vwap_wave",
        "setup": "vwap_bounce",
        "symbol": "BTCUSD",
        "role": "exit",
        "uuid": parsed["uuid"],
    }
    assert re.fullmatch(r"[0-9a-f]{8}", parsed["uuid"])


def test_parse_returns_none_for_non_aitrader_prefix():
    assert parse_client_order_id("foo__vwap_wave__bounce__AAPL__entry__abcd1234") is None
    assert parse_client_order_id("AITRADER__vwap_wave__bounce__AAPL__entry__abcd1234") is None
    assert parse_client_order_id("") is None
    assert parse_client_order_id(None) is None  # type: ignore[arg-type]


def test_parse_returns_none_for_bad_segment_count():
    # Missing role + uuid
    assert parse_client_order_id("aitrader__vwap_wave__bounce__AAPL") is None
    # Extra trailing segment
    assert parse_client_order_id("aitrader__vwap_wave__bounce__AAPL__entry__abcd1234__extra") is None


def test_parse_returns_none_for_unknown_role():
    bad = "aitrader__vwap_wave__bounce__AAPL__rebalance__abcd1234"
    assert parse_client_order_id(bad) is None


def test_parse_returns_none_for_bad_uuid():
    bad = "aitrader__vwap_wave__bounce__AAPL__entry__nothex12"
    assert parse_client_order_id(bad) is None


def test_parse_returns_none_for_empty_segment():
    bad = "aitrader__vwap_wave____AAPL__entry__abcd1234"  # empty setup
    assert parse_client_order_id(bad) is None
