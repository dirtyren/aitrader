import pytest

from arbitrage.paper.fill_engine import simulate_fill
from arbitrage.paper.ledger import Ledger
from arbitrage.paper.order_sink import Fill, LiveSink, PaperSink, Signal


def test_live_sink_raises():
    with pytest.raises(NotImplementedError):
        LiveSink()


def test_simulate_fill_applies_slippage():
    signal = Signal("BTC/USDT", "A", "B", 1000.0, 5.0, 0)
    book = {"A": {"bid": 100.0, "ask": 100.5}, "B": {"bid": 101.0, "ask": 101.5}}
    fill = simulate_fill(signal, book, latency_ms=0, slippage_bps=1.0, fee_bps=0.0)
    assert fill.buy_price == pytest.approx(100.5 * 1.0001)
    assert fill.sell_price == pytest.approx(101.0 * 0.9999)


def test_simulate_fill_fee_pnl_and_latency():
    signal = Signal("BTC/USDT", "A", "B", 1000.0, 5.0, 10)
    book = {"A": {"bid": 99.0, "ask": 100.0}, "B": {"bid": 101.0, "ask": 102.0}}
    fill = simulate_fill(signal, book, latency_ms=50, slippage_bps=0.0, fee_bps=10.0)
    assert fill.fee_usd == pytest.approx(2.0)  # 10 bps on 1000 notional, both legs
    assert fill.pnl_usd == pytest.approx(10.0 * 1.0 - 2.0)  # qty 10 * (101-100) - fees
    assert fill.ts_utc_ms == 60
    assert fill.signal is signal


def test_paper_sink_uses_venue_taker_fees():
    sink = PaperSink({"A": 10.0, "B": 20.0}, latency_ms=0, slippage_bps=0.0)
    signal = Signal("BTC/USDT", "A", "B", 1000.0, 5.0, 0)
    book = {"A": {"bid": 99.0, "ask": 100.0}, "B": {"bid": 101.0, "ask": 102.0}}
    fill = sink.submit(signal, book)
    assert fill.fee_usd == pytest.approx(1000 * 10 / 1e4 + 1000 * 20 / 1e4)


def test_ledger_pnl():
    led = Ledger()
    led.record(Fill(Signal("BTC/USDT", "A", "B", 1000.0, 5.0, 0), 100.0, 101.0, 1.0, 5.0, 0))
    assert led.realized_pnl_usd() == 5.0
    assert led.round_trips() == 1
