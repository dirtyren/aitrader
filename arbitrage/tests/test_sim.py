import asyncio
import json

import pytest

from arbitrage.config import load_config
from arbitrage.paper.ledger import Ledger
from arbitrage.paper.order_sink import LiveSink, PaperSink
from arbitrage.paper.resource_meter import ResourceMeter
from arbitrage.paper.sim import Simulator


def _book(ts, a_bid, a_ask, b_bid, b_ask, pair="BTC/USDT", a="A", b="B"):
    return {
        "ts_utc_ms": ts,
        "pair": pair,
        "books": {
            a: {"bid": a_bid, "ask": a_ask},
            b: {"bid": b_bid, "ask": b_ask},
        },
    }


def _source(items):
    async def gen():
        for it in items:
            yield it

    return gen()


class RecordingSink:
    def __init__(self):
        self.signals = []

    def submit(self, signal, book):
        self.signals.append(signal)
        return PaperSink({"A": 0.0, "B": 0.0}).submit(signal, book)


def test_resource_meter_snapshot():
    m = ResourceMeter()
    m.record_quote_latency_ms("binance", 12)
    m.record_quote_latency_ms("binance", 20)
    m.record_ws_message("binance")
    m.record_api_call("binance")
    s = m.snapshot()
    assert s["binance"]["latency_p50_ms"] == 12.0
    assert s["binance"]["latency_p95_ms"] == 20.0
    assert s["binance"]["ws_messages"] == 1
    assert s["binance"]["api_calls"] == 1
    assert "cpu_seconds" in s["process"] and "max_rss_kb" in s["process"]


def test_sim_writes_live_state_and_uses_paper_sink(tmp_path):
    cfg = load_config()
    assert cfg.test_mode is True
    led = Ledger()
    path = tmp_path / "live_state.json"
    sim = Simulator(cfg, None, ResourceMeter(), led, path,
                    source=_source([_book(1, 100, 100.1, 100.2, 100.3, a="binance", b="okx")]))
    assert isinstance(sim.sink, PaperSink)
    asyncio.run(sim.run())
    data = json.loads(path.read_text())
    assert data["test_mode"] is True
    assert "resources" in data and "pnl_usd" in data and "spread_bps" in data
    assert not list(tmp_path.glob("*.tmp"))


def test_sim_refuses_live_sink_in_test_mode(tmp_path):
    class FakeLive(LiveSink):
        def __init__(self):  # bypass the stub's raising constructor
            pass

    with pytest.raises(ValueError):
        Simulator(load_config(), FakeLive(), ResourceMeter(), Ledger(), tmp_path / "s.json")


def test_sim_signals_only_above_trigger_and_records_pnl(tmp_path):
    sink = RecordingSink()
    led = Ledger()
    items = [
        _book(1, 100, 100.1, 100.0, 100.1),   # no spread
        _book(2, 100, 100.0, 101.0, 101.1),   # buy A @100, sell B @101: ~100 bps
    ]
    sim = Simulator(load_config(), sink, ResourceMeter(), led, tmp_path / "s.json",
                    source=_source(items), trigger_bps=10.0, size_usd=1000.0)
    asyncio.run(sim.run())
    assert len(sink.signals) == 1
    s = sink.signals[0]
    assert (s.venue_buy, s.venue_sell, s.ts_utc_ms) == ("A", "B", 2)
    assert s.gross_bps == pytest.approx(100.0)
    assert led.round_trips() == 1
    assert json.loads((tmp_path / "s.json").read_text())["pnl_usd"] == pytest.approx(
        led.realized_pnl_usd())


def test_sim_state_write_is_throttled(tmp_path):
    t = [0.0]
    writes = []
    path = tmp_path / "s.json"
    items = [_book(i, 100, 100.1, 100.0, 100.1) for i in range(5)]

    sim = Simulator(load_config(), RecordingSink(), ResourceMeter(), Ledger(), path,
                    source=_source(items), clock=lambda: t[0])
    orig = sim._write_state

    def counting(*a, **k):
        writes.append(1)
        return orig(*a, **k)

    sim._write_state = counting
    asyncio.run(sim.run())
    # first tick writes, next 4 ticks (same clock) throttled, plus one final flush
    assert len(writes) == 2
