import os
from datetime import datetime, timezone
from unittest.mock import MagicMock
import pytest

os.environ.setdefault("ALPACA_API_KEY", "test")
os.environ.setdefault("ALPACA_SECRET_KEY", "test")

from broker.alpaca_data import AlpacaData
from core.bar import Bar


@pytest.fixture
def fake_client():
    client = MagicMock()
    client.get_stock_bars.return_value = [
        {"t": "2026-05-14T13:30:00Z", "o": 100.0, "h": 101.0, "l": 99.0, "c": 100.5, "v": 1000.0},
        {"t": "2026-05-14T13:35:00Z", "o": 100.5, "h": 102.0, "l": 100.0, "c": 101.5, "v": 1200.0},
    ]
    client.get_crypto_bars.return_value = [
        {"t": "2026-05-14T00:00:00Z", "o": 50000.0, "h": 50500.0, "l": 49800.0, "c": 50200.0, "v": 12.5}
    ]
    return client


def test_get_bars_equity(fake_client, tmp_path):
    data = AlpacaData(fake_client, cache_dir=str(tmp_path))
    bars = data.get_bars("AAPL", "equity", "5Min",
                        start=datetime(2026, 5, 14, 13, 30, tzinfo=timezone.utc),
                        end=datetime(2026, 5, 14, 13, 40, tzinfo=timezone.utc))
    assert len(bars) == 2
    assert isinstance(bars[0], Bar)
    assert bars[0].symbol == "AAPL"
    assert bars[0].close == 100.5


def test_get_bars_crypto(fake_client, tmp_path):
    data = AlpacaData(fake_client, cache_dir=str(tmp_path))
    bars = data.get_bars("BTC/USD", "crypto", "5Min",
                        start=datetime(2026, 5, 14, 0, 0, tzinfo=timezone.utc),
                        end=datetime(2026, 5, 14, 0, 5, tzinfo=timezone.utc))
    assert len(bars) == 1
    assert bars[0].symbol == "BTC/USD"
    assert bars[0].close == 50200.0


def test_cache_avoids_second_call(fake_client, tmp_path):
    data = AlpacaData(fake_client, cache_dir=str(tmp_path))
    args = ("AAPL", "equity", "5Min",
            datetime(2026, 5, 14, 13, 30, tzinfo=timezone.utc),
            datetime(2026, 5, 14, 13, 40, tzinfo=timezone.utc))
    data.get_bars(*args, use_cache=True)
    data.get_bars(*args, use_cache=True)
    assert fake_client.get_stock_bars.call_count == 1
