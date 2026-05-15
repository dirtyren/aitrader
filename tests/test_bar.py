from datetime import datetime, timezone
import pytest
from core.bar import Bar


def test_bar_immutable_and_validates():
    b = Bar(
        symbol="AAPL",
        ts=datetime(2026, 5, 14, 14, 35, tzinfo=timezone.utc),
        open=100.0, high=101.0, low=99.5, close=100.5, volume=1234,
    )
    assert b.symbol == "AAPL"
    assert b.range == 1.5
    assert b.is_bullish
    with pytest.raises(AttributeError):
        b.close = 200.0  # frozen


def test_bar_rejects_invalid_ohlc():
    with pytest.raises(ValueError):
        Bar(symbol="AAPL", ts=datetime.now(timezone.utc),
            open=100.0, high=99.0, low=99.5, close=100.0, volume=1)


def test_bar_rejects_naive_ts():
    with pytest.raises(ValueError, match="timezone-aware"):
        Bar(symbol="AAPL", ts=datetime(2026, 5, 14, 14, 35),
            open=100.0, high=101.0, low=99.5, close=100.5, volume=1234)
