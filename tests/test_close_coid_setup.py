"""Close COID must carry the real setup name so reconciler/fills.py
apply_tagged_fill can match it back to the open MySQL row.

Pre-fix bug: close_position hardcoded setup="_unknown", so the close fill's
COID parsed to setup="unknown", find_open_position_by_setup returned None,
and the open row stayed open forever — driving the COIN incident on
2026-06-02.
"""
from __future__ import annotations
from unittest.mock import MagicMock

import pytest

from broker.client_order_id import parse_client_order_id
from broker.order_executor import OrderExecutor
from state.position_book import PositionBook


def test_close_position_coid_carries_real_setup():
    client = MagicMock()
    client.submit_order.return_value = {"id": "ord-xyz"}
    book = PositionBook()
    ex = OrderExecutor(client, book, strategy_name="vwap_wave_equity",
                       logger=MagicMock(), mysql_store=None)

    ex.close_position(
        symbol="COIN", side="short", qty=1.0,
        setup="price_discovery", asset_class="equity",
    )

    assert client.submit_order.called
    submitted = client.submit_order.call_args
    coid = submitted.kwargs.get("client_order_id")
    if coid is None:
        # Some clients pass coid positionally; check positional args too.
        # If neither, the test will rightly fail.
        coid = next((a for a in submitted.args if isinstance(a, str)
                     and "__" in a), None)
    assert coid is not None
    parsed = parse_client_order_id(coid)
    assert parsed is not None
    assert parsed["strategy"] == "vwap_wave_equity"
    assert parsed["setup"] == "price_discovery"
    assert parsed["symbol"] == "COIN"
    # Role.EXIT serializes to "X" per broker/client_order_id.py
    assert parsed["role"] in ("X", "exit")


def test_close_position_setup_is_required_keyword():
    client = MagicMock()
    book = PositionBook()
    ex = OrderExecutor(client, book, strategy_name="vwap_wave_equity",
                       logger=MagicMock(), mysql_store=None)

    with pytest.raises(TypeError):
        ex.close_position(symbol="COIN", side="short", qty=1.0,
                          asset_class="equity")  # missing setup
