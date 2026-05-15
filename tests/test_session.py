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


def test_session_in_value_fraction_running_bands():
    """Verifies in_value_area_fraction uses LIVE bands (incremental), not final/static.

    Trending bars: 20 bars marching up. Under running bands, only the first
    couple of bars are 'inside' (because the bands tighten around the early
    cluster). After several bars trend keeps closes outside the running ±1σ.
    Fraction should be small (well below 0.5).
    """
    ctx = SessionContext(symbol="BTC/USD", asset_class=CRYPTO)
    base = datetime(2026, 5, 14, 0, 5, tzinfo=timezone.utc)
    for i in range(20):
        c = 100 + i * 1.0
        bar = Bar(symbol="BTC/USD", ts=base + timedelta(minutes=5*i),
                  open=c - 0.5, high=c + 0.5, low=c - 0.5, close=c, volume=10)
        ctx.ingest(bar)
    fraction = ctx.in_value_area_fraction()
    assert fraction < 0.30, f"trending fraction was {fraction}, expected <0.30"


def test_session_in_value_fraction_balanced():
    """Verifies the balanced case: bars hugging the mean stay 'inside' running bands."""
    ctx = SessionContext(symbol="BTC/USD", asset_class=CRYPTO)
    base = datetime(2026, 5, 14, 0, 5, tzinfo=timezone.utc)
    for i in range(20):
        # All bars at price 100 — zero variance → bands collapse to 100 → close=100 always inside
        bar = Bar(symbol="BTC/USD", ts=base + timedelta(minutes=5*i),
                  open=100, high=100.5, low=99.5, close=100, volume=10)
        ctx.ingest(bar)
    assert ctx.in_value_area_fraction() == 1.0
