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


def test_get_stock_bars_rejects_naive_datetime():
    client = AlpacaClient()
    with pytest.raises(ValueError, match="timezone-aware"):
        client.get_stock_bars("AAPL", "5Min",
                               start=datetime(2026, 5, 14, 13, 30),
                               end=datetime(2026, 5, 14, 14, 0, tzinfo=timezone.utc))


def test_get_crypto_bars_rejects_naive_datetime():
    client = AlpacaClient()
    with pytest.raises(ValueError, match="timezone-aware"):
        client.get_crypto_bars("BTC/USD", "5Min",
                                 start=datetime(2026, 5, 14, 0, 0, tzinfo=timezone.utc),
                                 end=datetime(2026, 5, 14, 0, 30))


def test_get_stock_bars_paginates_via_next_page_token():
    """Without pagination Alpaca silently truncates at one page (~10k bars).
    The fetcher must follow next_page_token until it's absent."""
    client = AlpacaClient()
    page1 = {
        "bars": [{"t": "2026-05-14T13:30:00Z", "o": 1, "h": 1, "l": 1, "c": 1, "v": 1}],
        "next_page_token": "tok-2",
    }
    page2 = {
        "bars": [{"t": "2026-05-14T13:35:00Z", "o": 2, "h": 2, "l": 2, "c": 2, "v": 2}],
        "next_page_token": "tok-3",
    }
    page3 = {
        "bars": [{"t": "2026-05-14T13:40:00Z", "o": 3, "h": 3, "l": 3, "c": 3, "v": 3}],
        "next_page_token": None,
    }
    responses = [_mock_response(200, p) for p in (page1, page2, page3)]
    with patch.object(client._session, "request", side_effect=responses) as req:
        bars = client.get_stock_bars(
            "AAPL", "1Hour",
            start=datetime(2026, 5, 14, 13, 30, tzinfo=timezone.utc),
            end=datetime(2026, 5, 14, 14, 0, tzinfo=timezone.utc),
        )
    assert len(bars) == 3
    assert [b["c"] for b in bars] == [1, 2, 3]
    assert req.call_count == 3
    # Final call's params dict reflects the last token applied (mutation
    # in place — MagicMock records by reference). Verifying the call count
    # plus accumulated bars proves pagination ran end-to-end; the last
    # request's params should carry tok-3 (set just before page3 fetch).
    last_params = req.call_args_list[-1].kwargs.get("params", {})
    assert last_params.get("page_token") == "tok-3"


def test_get_crypto_bars_paginates_via_next_page_token():
    client = AlpacaClient()
    page1 = {"bars": {"BTC/USD": [
        {"t": "2026-05-14T00:00:00Z", "o": 1, "h": 1, "l": 1, "c": 1, "v": 1}
    ]}, "next_page_token": "tok-2"}
    page2 = {"bars": {"BTC/USD": [
        {"t": "2026-05-14T00:05:00Z", "o": 2, "h": 2, "l": 2, "c": 2, "v": 2}
    ]}, "next_page_token": None}
    responses = [_mock_response(200, p) for p in (page1, page2)]
    with patch.object(client._session, "request", side_effect=responses) as req:
        bars = client.get_crypto_bars(
            "BTC/USD", "5Min",
            start=datetime(2026, 5, 14, 0, 0, tzinfo=timezone.utc),
            end=datetime(2026, 5, 14, 0, 30, tzinfo=timezone.utc),
        )
    assert len(bars) == 2
    assert req.call_count == 2
