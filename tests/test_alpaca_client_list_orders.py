from unittest.mock import MagicMock
import pytest
from broker.alpaca_client import AlpacaClient


def _make_client_with_response(payload):
    client = AlpacaClient.__new__(AlpacaClient)  # bypass network init
    fake_response = MagicMock()
    fake_response.json.return_value = payload
    fake_response.status_code = 200
    client._request = MagicMock(return_value=fake_response)
    return client


def test_list_orders_default_status_open():
    client = _make_client_with_response([])
    client.list_orders()
    args, kwargs = client._request.call_args
    assert args[0] == "GET"
    assert args[1] == "/v2/orders"
    assert kwargs["params"]["status"] == "open"
    assert kwargs["params"]["nested"] == "true"
    assert "symbols" not in kwargs["params"]


def test_list_orders_with_symbols_filter():
    client = _make_client_with_response([])
    client.list_orders(symbols=["AAPL", "MSFT"])
    _, kwargs = client._request.call_args
    assert kwargs["params"]["symbols"] == "AAPL,MSFT"


def test_list_orders_status_override():
    client = _make_client_with_response([])
    client.list_orders(status="closed")
    _, kwargs = client._request.call_args
    assert kwargs["params"]["status"] == "closed"


def test_list_orders_nested_false():
    client = _make_client_with_response([])
    client.list_orders(nested=False)
    _, kwargs = client._request.call_args
    assert kwargs["params"]["nested"] == "false"


def test_list_orders_returns_parsed_json():
    payload = [{"id": "o1", "symbol": "AAPL", "type": "limit"}]
    client = _make_client_with_response(payload)
    result = client.list_orders()
    assert result == payload


def test_list_orders_forwards_after_parameter():
    """`after` is sent as the `after` query param when provided."""
    from datetime import datetime, timezone
    client = _make_client_with_response([])
    after_ts = datetime(2026, 5, 28, 14, 0, tzinfo=timezone.utc)
    client.list_orders(status="closed", after=after_ts, nested=True)
    _, kwargs = client._request.call_args
    params = kwargs["params"]
    # Alpaca expects ISO 8601 — confirm a string starting with the date
    assert "after" in params
    assert params["after"].startswith("2026-05-28T14:00:00")


def test_list_orders_omits_after_when_not_provided():
    client = _make_client_with_response([])
    client.list_orders(status="open")
    _, kwargs = client._request.call_args
    params = kwargs["params"]
    assert "after" not in params
