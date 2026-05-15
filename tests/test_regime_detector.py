from datetime import datetime, timezone, timedelta
from core.bar import Bar
from core.session import SessionContext
from core.asset_class import AssetClassConfig
from strategies.regime_detector import RegimeDetector, RegimeConfig


CRYPTO = AssetClassConfig(
    name="crypto", timezone="UTC",
    session_open_local="00:00", session_close_local="23:59",
    opening_blackout_min=15, bar_timeframe="5Min",
    slippage_bps=5.0, commission_per_share=0.0, commission_bps=25.0,
)
CFG = RegimeConfig(trend_day_range_mult=1.5, trend_day_in_value_max=0.30,
                   balance_day_in_value_min=0.60)


def _b(ts, o, h, l, c):
    return Bar(symbol="X", ts=ts, open=o, high=h, low=l, close=c, volume=10)


def _ctx(bars, avg_range_20d):
    ctx = SessionContext(symbol="X", asset_class=CRYPTO)
    for b in bars:
        ctx.ingest(b)
    ctx.avg_range_20d = avg_range_20d
    return ctx


def test_balance_day_classified_as_range():
    base = datetime(2026, 5, 14, 0, 5, tzinfo=timezone.utc)
    bars = [_b(base + timedelta(minutes=5*i), 100, 100.5, 99.5, 100) for i in range(20)]
    ctx = _ctx(bars, avg_range_20d=2.0)
    assert RegimeDetector(CFG).classify(ctx) == "Range"


def test_trend_day_classified_as_trend():
    base = datetime(2026, 5, 14, 0, 5, tzinfo=timezone.utc)
    bars = []
    for i in range(20):
        c = 100 + i * 1.0
        bars.append(_b(base + timedelta(minutes=5*i), c - 0.5, c + 0.5, c - 0.5, c))
    ctx = _ctx(bars, avg_range_20d=2.0)
    assert RegimeDetector(CFG).classify(ctx) == "Trend"


def test_undefined_when_too_few_bars():
    ctx = _ctx([], avg_range_20d=2.0)
    assert RegimeDetector(CFG).classify(ctx) == "Undefined"
