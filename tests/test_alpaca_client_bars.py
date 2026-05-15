from datetime import datetime, timezone
from unittest.mock import patch, MagicMock
import os
import pytest

os.environ.setdefault("ALPACA_API_KEY", "test")
os.environ.setdefault("ALPACA_SECRET_KEY", "test")

from broker.alpaca_client import AlpacaClient


def _mock_response(status: int, body: dict) -> MagicMock:
    m = MagicMock()
    m.status_code = status
    m.json.return_value = body
    return m


def test_get_stock_bars():
    client = AlpacaClient()
    body = {
        "bars": [
            {"t": "2026-05-14T13:30:00Z", "o": 100, "h": 101, "l": 99, "c": 100.5, "v": 1000},
            {"t": "2026-05-14T13:35:00Z", "o": 100.5, "h": 102, "l": 100, "c": 101.5, "v": 1200},
        ]
    }
    with patch.object(client._session, "request", return_value=_mock_response(200, body)) as req:
        bars = client.get_stock_bars("AAPL", "5Min",
                                     start=datetime(2026, 5, 14, 13, 30, tzinfo=timezone.utc),
                                     end=datetime(2026, 5, 14, 14, 0, tzinfo=timezone.utc))
        assert len(bars) == 2
        assert bars[0]["c"] == 100.5
        url = req.call_args[0][1]
        assert "data.alpaca.markets" in url
        assert "AAPL" in url


def test_get_crypto_bars():
    client = AlpacaClient()
    body = {"bars": {"BTC/USD": [
        {"t": "2026-05-14T00:00:00Z", "o": 50000, "h": 50500, "l": 49800, "c": 50200, "v": 12.5}
    ]}}
    with patch.object(client._session, "request", return_value=_mock_response(200, body)) as req:
        bars = client.get_crypto_bars("BTC/USD", "5Min",
                                      start=datetime(2026, 5, 14, 0, 0, tzinfo=timezone.utc),
                                      end=datetime(2026, 5, 14, 0, 30, tzinfo=timezone.utc))
        assert len(bars) == 1
        url = req.call_args[0][1]
        assert "v1beta3/crypto" in url
