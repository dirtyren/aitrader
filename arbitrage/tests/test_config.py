from datetime import datetime, timezone
from dataclasses import FrozenInstanceError
import pytest
import pandas as pd

from arbitrage.config import Config, ExchangeConfig, load_config
from arbitrage.connectors.base import Quote, VenueConnector


def test_load_config_defaults():
    cfg = load_config()
    assert cfg.test_mode is True
    assert cfg.alignment_grid_ms == 10
    assert cfg.staleness_ms == 1000
    assert cfg.slippage_bps_grid == (0, 1, 2, 5, 10)
    assert cfg.leg_risk_grid == (0.0, 0.1, 0.5, 1.0)
    assert cfg.latency_ms_grid == (10, 50, 100, 250)
    assert cfg.reference_notional_usd == (10000, 100000)
    assert cfg.adverse_move_bps == 5.0
    assert cfg.rebalancing_bps == 2.0
    assert cfg.go_nogo_min_slippage_bps == 1.0
    assert cfg.go_nogo_min_persistence_ms == 1000
    assert cfg.pairs == ("BTC/USDT", "ETH/USDT")

    # Exchange taker fees (bps): binance 10, okx 10, bybit 10, kraken 26, coinbase 60
    assert cfg.exchanges["binance"].taker_fee_bps == 10.0
    assert cfg.exchanges["okx"].taker_fee_bps == 10.0
    assert cfg.exchanges["bybit"].taker_fee_bps == 10.0
    assert cfg.exchanges["kraken"].taker_fee_bps == 26.0
    assert cfg.exchanges["coinbase"].taker_fee_bps == 60.0
    assert cfg.exchanges["binance"].maker_fee_bps == 0.0


def test_config_missing_file_raises():
    with pytest.raises(FileNotFoundError):
        load_config("arbitrage/does_not_exist.yaml")


def test_config_is_frozen():
    cfg = load_config()
    with pytest.raises(FrozenInstanceError):
        cfg.test_mode = False  # type: ignore[misc]


def test_exchange_config():
    ec = ExchangeConfig(name="binance", taker_fee_bps=10.0)
    assert ec.name == "binance"
    assert ec.taker_fee_bps == 10.0
    assert ec.maker_fee_bps == 0.0
    with pytest.raises(FrozenInstanceError):
        ec.taker_fee_bps = 5.0  # type: ignore[misc]


def test_quote_frozen_dataclass():
    q = Quote(
        venue="binance",
        symbol="BTC/USDT",
        ts_utc_ms=1700000000000,
        bid=50000.0,
        ask=50001.0,
    )
    assert q.venue == "binance"
    assert q.symbol == "BTC/USDT"
    assert q.ts_utc_ms == 1700000000000
    assert q.bid == 50000.0
    assert q.ask == 50001.0
    with pytest.raises(FrozenInstanceError):
        q.bid = 50002.0  # type: ignore[misc]


def test_venue_connector_abc():
    # VenueConnector is abstract and cannot be instantiated directly
    with pytest.raises(TypeError):
        VenueConnector()  # type: ignore[abstract]

    class DummyConnector(VenueConnector):
        def load_quotes(
            self, symbol: str, start: datetime, end: datetime
        ) -> pd.DataFrame:
            return pd.DataFrame(
                [
                    {"ts_utc_ms": 1000, "bid": 100.0, "ask": 100.1},
                    {"ts_utc_ms": 2000, "bid": 100.2, "ask": 100.3},
                ]
            )

    connector = DummyConnector()
    df = connector.load_quotes(
        "BTC/USDT",
        datetime(2026, 1, 1, tzinfo=timezone.utc),
        datetime(2026, 1, 2, tzinfo=timezone.utc),
    )
    assert list(df.columns) == ["ts_utc_ms", "bid", "ask"]
    assert len(df) == 2


def test_package_exports():
    import arbitrage

    assert hasattr(arbitrage, "Config")
    assert hasattr(arbitrage, "ExchangeConfig")
    assert hasattr(arbitrage, "load_config")
    assert hasattr(arbitrage, "Quote")
    assert hasattr(arbitrage, "VenueConnector")


def test_load_custom_config(tmp_path):
    custom_yaml = tmp_path / "custom.yaml"
    custom_yaml.write_text(
        """
test_mode: false
pairs:
  - "BTC/USDT"
alignment_grid_ms: 20
staleness_ms: 500
slippage_bps_grid: [1.0, 3.0]
leg_risk_grid: [0.05]
latency_ms_grid: [25]
reference_notional_usd: [50000]
adverse_move_bps: 4.0
rebalancing_bps: 1.5
go_nogo_min_slippage_bps: 2.0
go_nogo_min_persistence_ms: 500
exchanges:
  custom_ex:
    name: custom_ex
    taker_fee_bps: 15.0
    maker_fee_bps: 5.0
"""
    )
    cfg = load_config(str(custom_yaml))
    assert cfg.test_mode is False
    assert cfg.pairs == ("BTC/USDT",)
    assert cfg.alignment_grid_ms == 20
    assert cfg.staleness_ms == 500
    assert cfg.slippage_bps_grid == (1.0, 3.0)
    assert cfg.leg_risk_grid == (0.05,)
    assert cfg.latency_ms_grid == (25,)
    assert cfg.reference_notional_usd == (50000.0,)
    assert cfg.adverse_move_bps == 4.0
    assert cfg.rebalancing_bps == 1.5
    assert cfg.go_nogo_min_slippage_bps == 2.0
    assert cfg.go_nogo_min_persistence_ms == 500
    assert "custom_ex" in cfg.exchanges
    assert cfg.exchanges["custom_ex"].taker_fee_bps == 15.0
    assert cfg.exchanges["custom_ex"].maker_fee_bps == 5.0

