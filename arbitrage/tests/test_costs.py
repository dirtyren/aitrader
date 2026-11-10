import pytest

from arbitrage.config import load_config
from arbitrage.costs import fee_bps, round_trip_cost_bps


def test_fee_bps_units():
    cfg = load_config()
    assert fee_bps("binance", cfg) == 10.0  # 10 bps, NOT 0.10
    assert fee_bps("okx", cfg) == 10.0
    assert fee_bps("bybit", cfg) == 10.0
    assert fee_bps("kraken", cfg) == 26.0
    assert fee_bps("coinbase", cfg) == 60.0


def test_fee_bps_unknown_exchange_raises():
    cfg = load_config()
    with pytest.raises(KeyError):
        fee_bps("non_existent_exchange", cfg)


def test_round_trip_cost_arithmetic():
    cfg = load_config()
    # binance(10) + okx(10) + slippage 2 + leg_risk 0.5*5 + rebal 2 = 26.5
    cost = round_trip_cost_bps(cfg, "binance", "okx", 2.0, 0.5, 5.0, 2.0)
    assert cost == pytest.approx(26.5)


def test_round_trip_cost_asymmetric_venues():
    cfg = load_config()
    # binance(10) + coinbase(60) + slippage 1.0 + leg_risk 0.1*5.0 (0.5) + rebal 2.0 = 73.5
    cost = round_trip_cost_bps(cfg, "binance", "coinbase", 1.0, 0.1, 5.0, 2.0)
    assert cost == pytest.approx(73.5)


def test_round_trip_cost_zero_components():
    cfg = load_config()
    # binance(10) + okx(10) + 0 + 0*0 + 0 = 20.0
    cost = round_trip_cost_bps(cfg, "binance", "okx", 0.0, 0.0, 0.0, 0.0)
    assert cost == pytest.approx(20.0)


def test_round_trip_cost_unknown_venue_raises():
    cfg = load_config()
    with pytest.raises(KeyError):
        round_trip_cost_bps(cfg, "unknown", "binance", 2.0, 0.5, 5.0, 2.0)
    with pytest.raises(KeyError):
        round_trip_cost_bps(cfg, "binance", "unknown", 2.0, 0.5, 5.0, 2.0)
