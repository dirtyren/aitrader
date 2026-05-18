from datetime import datetime, timezone, timedelta
from dataclasses import dataclass
from state.daily_ledger import DailyLedger, TradeRecord
from state.position_book import PositionBook, OpenPosition
from risk.filters import (
    FilterPipeline, FilterResult,
    SystemHaltedFilter, SessionWindowFilter, NewsBlackoutFilter,
    ConsecutiveLossFilter, ConcurrentPositionFilter,
    NewsBlackout,
)
from strategies.base_setup import SetupSignal


@dataclass
class FakeCB:
    level: int = 0


def _signal(symbol="AAPL"):
    return SetupSignal(setup="x", symbol=symbol, side="long",
                       entry=100, stop=99, target=102, atr=1.0,
                       level=100, ts=datetime(2026, 5, 14, 14, 0, tzinfo=timezone.utc))


def test_system_halted_blocks_when_cb_l2():
    f = SystemHaltedFilter(circuit_breaker=FakeCB(level=2), lock_file_path="/nonexistent")
    res = f.check(_signal(), ctx=None, ledger=None, book=None)
    assert not res.passed


def test_consecutive_loss_blocks_after_two():
    led = DailyLedger(initial_equity=100000)
    t = datetime(2026, 5, 14, 14, 0, tzinfo=timezone.utc)
    for _ in range(2):
        led.record(TradeRecord(symbol="AAPL", setup="x", entry_ts=t, exit_ts=t,
                               entry_px=100, exit_px=99, side="long", qty=10,
                               R_realized=-1.0, pnl_usd=-10))
    f = ConsecutiveLossFilter(limit=2, scope="per_symbol")
    assert not f.check(_signal("AAPL"), ctx=None, ledger=led, book=None).passed
    assert f.check(_signal("MSFT"), ctx=None, ledger=led, book=None).passed


def test_concurrent_position_filter():
    book = PositionBook()
    for s in ("AAPL", "MSFT", "TSLA"):
        book.add(OpenPosition(symbol=s, setup="x", side="long", qty=1,
                              entry_px=1.0, stop_px=0.5, target_px=2.0,
                              opened_at=datetime.now(timezone.utc), order_id="x"))
    f = ConcurrentPositionFilter(max_concurrent=3)
    assert not f.check(_signal("NVDA"), ctx=None, ledger=None, book=book).passed


def test_news_blackout_filter():
    now = datetime(2026, 5, 14, 14, 33, tzinfo=timezone.utc)
    win = NewsBlackout(start=datetime(2026, 5, 14, 14, 30, tzinfo=timezone.utc),
                       duration_min=10, label="CPI")
    f = NewsBlackoutFilter(windows=[win], pad_min=5, now_fn=lambda: now)
    assert not f.check(_signal(), ctx=None, ledger=None, book=None).passed


def test_pipeline_short_circuits_on_first_reject():
    led = DailyLedger(initial_equity=100000)
    t = datetime(2026, 5, 14, 14, 0, tzinfo=timezone.utc)
    for _ in range(2):
        led.record(TradeRecord(symbol="AAPL", setup="x", entry_ts=t, exit_ts=t,
                               entry_px=100, exit_px=99, side="long", qty=10,
                               R_realized=-1.0, pnl_usd=-10))
    pipeline = FilterPipeline([
        SystemHaltedFilter(circuit_breaker=FakeCB(level=0), lock_file_path="/nonexistent"),
        ConsecutiveLossFilter(limit=2, scope="per_symbol"),
        ConcurrentPositionFilter(max_concurrent=10),
    ])
    res = pipeline.check(_signal("AAPL"), ctx=None, ledger=led, book=PositionBook())
    assert not res.passed
    assert "consecutive" in res.reason.lower()
