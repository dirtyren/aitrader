import os
from unittest.mock import patch, MagicMock

os.environ.setdefault("ALPACA_API_KEY", "test")
os.environ.setdefault("ALPACA_SECRET_KEY", "test")

from broker.alpaca_client import AlpacaClient


def _resp(status, body):
    m = MagicMock()
    m.status_code = status
    m.json.return_value = body
    return m


def test_submit_bracket_order_payload():
    client = AlpacaClient()
    expected_id = "abc123"
    with patch.object(client._session, "request",
                       return_value=_resp(200, {"id": expected_id})) as req:
        order = client.submit_bracket_order(
            symbol="AAPL", qty=10, side="buy",
            limit_price=100.0, stop_loss=99.0, take_profit=102.0,
            time_in_force="day",
        )
        assert order["id"] == expected_id
        body = req.call_args[1]["json"]
        assert body["order_class"] == "bracket"
        assert body["stop_loss"]["stop_price"] == 99.0
        assert body["take_profit"]["limit_price"] == 102.0
        assert body["limit_price"] == 100.0


def test_submit_crypto_market_order_uses_notional_optional():
    client = AlpacaClient()
    with patch.object(client._session, "request",
                       return_value=_resp(200, {"id": "x"})) as req:
        client.submit_order(symbol="BTC/USD", qty=0.01, side="buy",
                            order_type="market", time_in_force="gtc")
        body = req.call_args[1]["json"]
        assert body["symbol"] == "BTC/USD"
        assert body["time_in_force"] == "gtc"


def test_replace_order_patches_only_provided_fields():
    client = AlpacaClient()
    with patch.object(client._session, "request",
                       return_value=_resp(200, {"id": "leg-1", "stop_price": 100.0})) as req:
        order = client.replace_order("leg-1", stop_price=100.0)
        assert order["id"] == "leg-1"
        method, url = req.call_args[0]
        assert method == "PATCH"
        assert url.endswith("/v2/orders/leg-1")
        body = req.call_args[1]["json"]
        assert body == {"stop_price": 100.0}


def test_replace_order_supports_multiple_fields():
    client = AlpacaClient()
    with patch.object(client._session, "request",
                       return_value=_resp(200, {"id": "ord-9"})):
        client.replace_order("ord-9", qty=5, limit_price=101.5,
                             stop_price=99.5, time_in_force="day")
        body = client._session.request.call_args[1]["json"]
        assert body == {
            "qty": 5,
            "limit_price": 101.5,
            "stop_price": 99.5,
            "time_in_force": "day",
        }


def test_replace_order_rejects_empty_update():
    client = AlpacaClient()
    try:
        client.replace_order("ord-9")
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError when no fields provided")


def test_submit_bracket_order_rounds_sub_penny_prices():
    client = AlpacaClient()
    with patch.object(client._session, "request",
                       return_value=_resp(200, {"id": "x"})) as req:
        client.submit_bracket_order(
            symbol="PLTR", qty=10, side="buy",
            limit_price=88.117777,
            stop_loss=85.123456789,
            take_profit=88.11253636949422,
        )
        body = req.call_args[1]["json"]
        assert body["limit_price"] == 88.12
        assert body["stop_loss"]["stop_price"] == 85.12
        assert body["take_profit"]["limit_price"] == 88.11


def test_submit_bracket_order_sub_dollar_uses_four_decimals():
    client = AlpacaClient()
    with patch.object(client._session, "request",
                       return_value=_resp(200, {"id": "x"})) as req:
        client.submit_bracket_order(
            symbol="PENNY", qty=100, side="buy",
            limit_price=0.123456789,
            stop_loss=0.111111111,
            take_profit=0.999949999,
        )
        body = req.call_args[1]["json"]
        assert body["limit_price"] == 0.1235
        assert body["stop_loss"]["stop_price"] == 0.1111
        assert body["take_profit"]["limit_price"] == 0.9999


def test_replace_order_rounds_sub_penny_prices():
    client = AlpacaClient()
    with patch.object(client._session, "request",
                       return_value=_resp(200, {"id": "leg-1"})) as req:
        client.replace_order("leg-1",
                             limit_price=42.987654321,
                             stop_price=41.111111111)
        body = req.call_args[1]["json"]
        assert body["limit_price"] == 42.99
        assert body["stop_price"] == 41.11
