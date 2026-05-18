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
