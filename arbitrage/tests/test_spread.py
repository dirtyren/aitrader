import pandas as pd
import pytest

from arbitrage.spread import gross_spread, mid_quote


def test_gross_spread_sign_and_value():
    aligned = pd.DataFrame({"bid_A": [100.0], "ask_A": [100.5], "bid_B": [101.0], "ask_B": [101.5]})
    # buy on A (pay ask_A), sell on B (receive bid_B) -> profit = bid_B - ask_A = 0.5
    assert gross_spread(aligned, "A", "B").iloc[0] == 0.5
    # reverse direction is a loss: buy B (pay ask_B=101.5), sell A (receive bid_A=100.0) -> 100.0 - 101.5 = -1.5
    # note: brief asserted -1.0, but bid_A - ask_B is mathematically -1.5
    assert gross_spread(aligned, "B", "A").iloc[0] == -1.5


def test_mid_quote():
    aligned = pd.DataFrame({"bid_A": [100.0], "ask_A": [101.0]})
    assert mid_quote(aligned, "A").iloc[0] == 100.5


def test_gross_spread_and_mid_quote_multi_row_series():
    aligned = pd.DataFrame({
        "bid_binance": [100.0, 102.0],
        "ask_binance": [100.2, 102.2],
        "bid_okx": [100.5, 101.8],
        "ask_okx": [100.7, 102.0],
    })
    # buy binance, sell okx: bid_okx - ask_binance
    spread = gross_spread(aligned, "binance", "okx")
    assert isinstance(spread, pd.Series)
    assert spread.tolist() == pytest.approx([0.3, -0.4])

    mid_binance = mid_quote(aligned, "binance")
    assert isinstance(mid_binance, pd.Series)
    assert mid_binance.tolist() == pytest.approx([100.1, 102.1])
