# tests/test_alpaca_bars_multi.py
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from broker.alpaca_client import AlpacaClient


def _resp(payload: dict) -> MagicMock:
    r = MagicMock()
    r.json.return_value = payload
    return r


def _client() -> AlpacaClient:
    c = AlpacaClient.__new__(AlpacaClient)  # bypass __init__ (needs credentials)
    c._data_request = MagicMock()
    return c


START = datetime(2026, 8, 28, 13, 30, tzinfo=timezone.utc)
END = datetime(2026, 8, 28, 14, 0, tzinfo=timezone.utc)


def test_returns_dict_keyed_by_symbol():
    c = _client()
    c._data_request.return_value = _resp({
        "bars": {
            "AAPL": [{"t": "2026-08-28T13:30:00Z", "o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 100}],
            "MSFT": [{"t": "2026-08-28T13:30:00Z", "o": 3, "h": 4, "l": 2.5, "c": 3.5, "v": 200}],
        },
    })
    out = c.get_stock_bars_multi(["AAPL", "MSFT"], "1Min", START, END)
    assert set(out) == {"AAPL", "MSFT"}
    assert out["AAPL"][0]["v"] == 100


def test_merges_pages_per_symbol():
    c = _client()
    c._data_request.side_effect = [
        _resp({"bars": {"AAPL": [{"v": 1}]}, "next_page_token": "tok"}),
        _resp({"bars": {"AAPL": [{"v": 2}], "MSFT": [{"v": 3}]}}),
    ]
    out = c.get_stock_bars_multi(["AAPL", "MSFT"], "1Min", START, END)
    assert [b["v"] for b in out["AAPL"]] == [1, 2]
    assert [b["v"] for b in out["MSFT"]] == [3]


def test_chunks_symbol_list():
    c = _client()
    c._data_request.return_value = _resp({"bars": {}})
    c.get_stock_bars_multi(["A", "B", "C"], "1Min", START, END, chunk_size=2)
    assert c._data_request.call_count == 2
    first = c._data_request.call_args_list[0].kwargs["params"]["symbols"]
    second = c._data_request.call_args_list[1].kwargs["params"]["symbols"]
    assert first == "A,B"
    assert second == "C"


def test_page_token_does_not_leak_between_chunks():
    c = _client()
    c._data_request.side_effect = [
        _resp({"bars": {"A": [{"v": 1}]}, "next_page_token": "tok"}),
        _resp({"bars": {"B": [{"v": 2}]}}),
        _resp({"bars": {"C": [{"v": 3}]}}),
    ]
    c.get_stock_bars_multi(["A", "B", "C"], "1Min", START, END, chunk_size=2)
    third = c._data_request.call_args_list[2].kwargs["params"]
    assert "page_token" not in third


def test_empty_symbols_short_circuits():
    c = _client()
    assert c.get_stock_bars_multi([], "1Min", START, END) == {}
    c._data_request.assert_not_called()


def test_naive_datetime_rejected():
    c = _client()
    with pytest.raises(ValueError, match="timezone-aware"):
        c.get_stock_bars_multi(["AAPL"], "1Min", datetime(2026, 8, 28, 13, 30), END)


def test_uses_iex_feed():
    c = _client()
    c._data_request.return_value = _resp({"bars": {}})
    c.get_stock_bars_multi(["AAPL"], "1Min", START, END)
    assert c._data_request.call_args.kwargs["params"]["feed"] == "iex"
