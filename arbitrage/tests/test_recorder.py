import asyncio

import pandas as pd

from arbitrage.config import Config, ExchangeConfig
from arbitrage.connectors.ccxt_venue import CcxtVenue
from arbitrage.recorder import Recorder


class FakeExchange:
    """Network-free stand-in for a ccxt.pro exchange."""

    def __init__(self, books):
        self._books = list(books)
        self.closed = False

    async def watch_order_book(self, symbol, limit=None):
        if not self._books:
            await asyncio.sleep(3600)
        return self._books.pop(0)

    async def close(self):
        self.closed = True


def _cfg(venues=("binance",), pairs=("BTC/USDT",)):
    return Config(
        exchanges={v: ExchangeConfig(name=v, taker_fee_bps=10.0) for v in venues},
        pairs=tuple(pairs),
        test_mode=True,
        alignment_grid_ms=10,
        staleness_ms=1000,
        slippage_bps_grid=(0,),
        leg_risk_grid=(0.0,),
        latency_ms_grid=(10,),
        reference_notional_usd=(10000,),
        adverse_move_bps=5.0,
        rebalancing_bps=2.0,
        go_nogo_min_slippage_bps=1.0,
        go_nogo_min_persistence_ms=1000,
    )


def _book(ts, bid, ask):
    return {"timestamp": ts, "bids": [[bid, 1.0], [bid - 1, 2.0]], "asks": [[ask, 1.0]]}


def test_normalize_symbol():
    assert CcxtVenue.normalize("XBT/USDT") == "BTC/USDT"
    assert CcxtVenue.normalize("BTC-USDT") == "BTC/USDT"


def test_recorder_writes_quotes(tmp_path):
    ex = FakeExchange([_book(1_700_000_000_000, 100.0, 101.0),
                       _book(1_700_000_000_050, 100.5, 101.5)])
    rec = Recorder(_cfg(), tmp_path, exchange_factory=lambda name: ex,
                   max_updates_per_stream=2)
    asyncio.run(rec.run())

    path = tmp_path / "binance" / "BTC_USDT.parquet"
    assert path.exists()
    df = pd.read_parquet(path)
    assert list(df.columns) == ["ts_utc_ms", "bid", "ask"]
    assert df["ts_utc_ms"].tolist() == [1_700_000_000_000, 1_700_000_000_050]
    assert df["bid"].tolist() == [100.0, 100.5]
    assert df["ask"].tolist() == [101.0, 101.5]
    assert ex.closed


def test_recorder_normalizes_symbol_and_skips_empty_books(tmp_path):
    ex = FakeExchange([{"timestamp": 1, "bids": [], "asks": []},
                       _book(1_700_000_000_000, 10.0, 11.0)])
    rec = Recorder(_cfg(pairs=("XBT-USDT",)), tmp_path,
                   exchange_factory=lambda name: ex, max_updates_per_stream=2)
    asyncio.run(rec.run())
    df = pd.read_parquet(tmp_path / "binance" / "BTC_USDT.parquet")
    assert len(df) == 1


def test_live_state(tmp_path):
    exs = {"binance": FakeExchange([_book(1, 1.0, 2.0), _book(2, 1.0, 2.0)]),
           "okx": FakeExchange([_book(3, 1.0, 2.0)])}
    rec = Recorder(_cfg(venues=("binance", "okx")), tmp_path,
                   exchange_factory=lambda name: exs[name], max_updates_per_stream=None)

    async def drive():
        task = asyncio.create_task(rec.run())
        for _ in range(100):
            await asyncio.sleep(0.01)
            s = rec.live_state()
            if s.get("binance", {}).get("rows") == 2 and s.get("okx", {}).get("rows") == 1:
                break
        rec.stop()
        await task

    asyncio.run(drive())
    state = rec.live_state()
    assert state["binance"]["rows"] == 2
    assert state["okx"]["rows"] == 1
    assert state["binance"]["last_quote_age_ms"] >= 0
    assert (tmp_path / "okx" / "BTC_USDT.parquet").exists()
    assert len(pd.read_parquet(tmp_path / "binance" / "BTC_USDT.parquet")) == 2


def test_live_state_before_data(tmp_path):
    rec = Recorder(_cfg(), tmp_path, exchange_factory=lambda name: FakeExchange([]))
    state = rec.live_state()
    assert state["binance"]["rows"] == 0
    assert state["binance"]["last_quote_age_ms"] is None
