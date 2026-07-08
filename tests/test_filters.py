from datetime import datetime, timezone, timedelta
from dataclasses import dataclass
from state.daily_ledger import DailyLedger, TradeRecord
from state.position_book import PositionBook, OpenPosition
from risk.filters import (
    BrokerPositionFilter,
    FilterPipeline, FilterResult,
    SessionWindowFilter, NewsBlackoutFilter,
    ConsecutiveLossFilter, ConcurrentPositionFilter,
    NewsBlackout,
)
from strategies.base_setup import SetupSignal


def _signal(symbol="AAPL"):
    return SetupSignal(setup="x", symbol=symbol, side="long",
                       entry=100, stop=99, target=102, atr=1.0,
                       level=100, ts=datetime(2026, 5, 14, 14, 0, tzinfo=timezone.utc))


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


class _FakeBroker:
    def __init__(self, positions):
        self._positions = positions
        self.calls = 0

    def get_positions(self):
        self.calls += 1
        return self._positions


def test_broker_position_filter_rejects_when_broker_holds_inventory():
    broker = _FakeBroker([{"symbol": "COIN", "qty": "22"}])
    f = BrokerPositionFilter(broker=broker, cache_ttl_s=30.0)
    res = f.check(_signal("COIN"), ctx=None, ledger=None, book=None)
    assert not res.passed
    assert "broker" in res.reason.lower()


def test_broker_position_filter_allows_when_broker_flat():
    broker = _FakeBroker([{"symbol": "AAPL", "qty": "10"}])
    f = BrokerPositionFilter(broker=broker, cache_ttl_s=30.0)
    assert f.check(_signal("COIN"), ctx=None, ledger=None, book=None).passed


def test_broker_position_filter_zero_qty_treated_as_flat():
    broker = _FakeBroker([{"symbol": "COIN", "qty": "0"}])
    f = BrokerPositionFilter(broker=broker, cache_ttl_s=30.0)
    assert f.check(_signal("COIN"), ctx=None, ledger=None, book=None).passed


def test_broker_position_filter_matches_slash_and_flat_form():
    broker = _FakeBroker([{"symbol": "BTC/USD", "qty": "0.5"}])
    f = BrokerPositionFilter(broker=broker, cache_ttl_s=30.0)
    assert not f.check(_signal("BTCUSD"), ctx=None, ledger=None, book=None).passed
    assert not f.check(_signal("BTC/USD"), ctx=None, ledger=None, book=None).passed


def test_broker_position_filter_caches_within_ttl():
    broker = _FakeBroker([])
    base = datetime(2026, 6, 3, 12, 0, tzinfo=timezone.utc)
    clock = [base]
    f = BrokerPositionFilter(broker=broker, cache_ttl_s=30.0,
                             now_fn=lambda: clock[0])
    f.check(_signal("AAPL"), ctx=None, ledger=None, book=None)
    clock[0] = base + timedelta(seconds=10)
    f.check(_signal("AAPL"), ctx=None, ledger=None, book=None)
    assert broker.calls == 1
    clock[0] = base + timedelta(seconds=45)
    f.check(_signal("AAPL"), ctx=None, ledger=None, book=None)
    assert broker.calls == 2


def test_broker_position_filter_fails_open_on_broker_error():
    class _ErrBroker:
        def get_positions(self):
            raise RuntimeError("boom")
    f = BrokerPositionFilter(broker=_ErrBroker(), cache_ttl_s=30.0)
    assert f.check(_signal("COIN"), ctx=None, ledger=None, book=None).passed


def test_pipeline_short_circuits_on_first_reject():
    led = DailyLedger(initial_equity=100000)
    t = datetime(2026, 5, 14, 14, 0, tzinfo=timezone.utc)
    for _ in range(2):
        led.record(TradeRecord(symbol="AAPL", setup="x", entry_ts=t, exit_ts=t,
                               entry_px=100, exit_px=99, side="long", qty=10,
                               R_realized=-1.0, pnl_usd=-10))
    pipeline = FilterPipeline([
        ConsecutiveLossFilter(limit=2, scope="per_symbol"),
        ConcurrentPositionFilter(max_concurrent=10),
    ])
    res = pipeline.check(_signal("AAPL"), ctx=None, ledger=led, book=PositionBook())
    assert not res.passed
    assert "consecutive" in res.reason.lower()


# ── ManualCloseCooldownFilter ───────────────────────────────────────────────

class _FakeStore:
    def __init__(self, cooldowns: list[dict]):
        self._cooldowns = cooldowns
        self.calls = 0

    def get_active_cooldowns(self, *, strategy_id, now):
        self.calls += 1
        return [c for c in self._cooldowns if c["strategy_id"] == strategy_id]


def test_manual_close_cooldown_filter_blocks_when_active():
    from risk.filters import ManualCloseCooldownFilter
    until = datetime(2026, 6, 3, 13, 0, tzinfo=timezone.utc)
    store = _FakeStore([{
        "strategy_id": 7, "symbol": "COIN", "asset_class": "equity",
        "cooldown_until": until,
    }])
    base = datetime(2026, 6, 3, 12, 30, tzinfo=timezone.utc)
    f = ManualCloseCooldownFilter(store=store, strategy_id=7,
                                   cache_ttl_s=30.0, now_fn=lambda: base)
    res = f.check(_signal("COIN"), ctx=None, ledger=None, book=None)
    assert not res.passed
    assert "manual close cooldown" in res.reason.lower()


def test_manual_close_cooldown_filter_allows_when_no_row():
    from risk.filters import ManualCloseCooldownFilter
    store = _FakeStore([])
    base = datetime(2026, 6, 3, 12, 30, tzinfo=timezone.utc)
    f = ManualCloseCooldownFilter(store=store, strategy_id=7,
                                   cache_ttl_s=30.0, now_fn=lambda: base)
    assert f.check(_signal("COIN"), ctx=None, ledger=None, book=None).passed


def test_manual_close_cooldown_filter_matches_flat_and_slash_form():
    from risk.filters import ManualCloseCooldownFilter
    until = datetime(2026, 6, 3, 13, 0, tzinfo=timezone.utc)
    store = _FakeStore([{
        "strategy_id": 7, "symbol": "BTCUSD", "asset_class": "crypto",
        "cooldown_until": until,
    }])
    base = datetime(2026, 6, 3, 12, 30, tzinfo=timezone.utc)
    f = ManualCloseCooldownFilter(store=store, strategy_id=7,
                                   cache_ttl_s=30.0, now_fn=lambda: base)
    assert not f.check(_signal("BTCUSD"), ctx=None, ledger=None, book=None).passed
    assert not f.check(_signal("BTC/USD"), ctx=None, ledger=None, book=None).passed


def test_manual_close_cooldown_filter_allows_when_expired_in_cache():
    """If the cache hasn't refreshed yet but the row's window has actually
    ended, the filter must not phantom-block."""
    from risk.filters import ManualCloseCooldownFilter
    until = datetime(2026, 6, 3, 12, 0, tzinfo=timezone.utc)
    store = _FakeStore([{
        "strategy_id": 7, "symbol": "COIN", "asset_class": "equity",
        "cooldown_until": until,
    }])
    after = datetime(2026, 6, 3, 12, 30, tzinfo=timezone.utc)
    f = ManualCloseCooldownFilter(store=store, strategy_id=7,
                                   cache_ttl_s=300.0, now_fn=lambda: after)
    assert f.check(_signal("COIN"), ctx=None, ledger=None, book=None).passed


def test_manual_close_cooldown_filter_caches_within_ttl():
    from risk.filters import ManualCloseCooldownFilter
    until = datetime(2026, 6, 3, 13, 0, tzinfo=timezone.utc)
    store = _FakeStore([{
        "strategy_id": 7, "symbol": "COIN", "asset_class": "equity",
        "cooldown_until": until,
    }])
    base = datetime(2026, 6, 3, 12, 30, tzinfo=timezone.utc)
    clock = [base]
    f = ManualCloseCooldownFilter(store=store, strategy_id=7,
                                   cache_ttl_s=30.0,
                                   now_fn=lambda: clock[0])
    f.check(_signal("COIN"), ctx=None, ledger=None, book=None)
    clock[0] = base + timedelta(seconds=10)
    f.check(_signal("COIN"), ctx=None, ledger=None, book=None)
    assert store.calls == 1
    clock[0] = base + timedelta(seconds=45)
    f.check(_signal("COIN"), ctx=None, ledger=None, book=None)
    assert store.calls == 2


def test_manual_close_cooldown_filter_fails_open_on_store_error():
    from risk.filters import ManualCloseCooldownFilter

    class _ErrStore:
        def get_active_cooldowns(self, *, strategy_id, now):
            raise RuntimeError("boom")

    f = ManualCloseCooldownFilter(store=_ErrStore(), strategy_id=7,
                                   cache_ttl_s=30.0)
    assert f.check(_signal("COIN"), ctx=None, ledger=None, book=None).passed
