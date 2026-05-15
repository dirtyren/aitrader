from datetime import datetime, timezone, timedelta
from core.bar import Bar
from core.session import SessionContext
from core.asset_class import AssetClassConfig


CRYPTO = AssetClassConfig(
    name="crypto", timezone="UTC",
    session_open_local="00:00", session_close_local="23:59",
    opening_blackout_min=15, bar_timeframe="5Min",
    slippage_bps=5.0, commission_per_share=0.0, commission_bps=25.0,
)


def _b(ts, c):
    return Bar(symbol="BTC/USD", ts=ts, open=c, high=c + 1, low=c - 1, close=c, volume=10)


def test_session_ingest_updates_vwap():
    ctx = SessionContext(symbol="BTC/USD", asset_class=CRYPTO)
    base = datetime(2026, 5, 14, 0, 5, tzinfo=timezone.utc)
    for i in range(3):
        ctx.ingest(_b(base + timedelta(minutes=5*i), 100 + i))
    assert ctx.bar_count == 3
    assert 100 < ctx.vwap < 102
    assert ctx.day_high == 103
    assert ctx.day_low == 99


def test_session_resets_at_boundary():
    ctx = SessionContext(symbol="BTC/USD", asset_class=CRYPTO)
    day1 = datetime(2026, 5, 14, 23, 55, tzinfo=timezone.utc)
    ctx.ingest(_b(day1, 100))
    assert ctx.bar_count == 1

    day2 = datetime(2026, 5, 15, 0, 5, tzinfo=timezone.utc)
    ctx.ingest(_b(day2, 200))
    assert ctx.bar_count == 1   # reset
    assert ctx.session_start_ts == datetime(2026, 5, 15, 0, 0, tzinfo=timezone.utc)
    assert ctx.vwap == 200


def test_session_in_value_area():
    ctx = SessionContext(symbol="BTC/USD", asset_class=CRYPTO)
    base = datetime(2026, 5, 14, 0, 5, tzinfo=timezone.utc)
    for i, p in enumerate([100, 100, 100, 100, 100]):
        ctx.ingest(_b(base + timedelta(minutes=5*i), p))
    # zero variance → bands collapse to vwap → "in value" tolerance check
    assert ctx.in_value_area(100.0)
