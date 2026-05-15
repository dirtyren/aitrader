from datetime import datetime, timezone, timedelta
from core.bar import Bar
from core.session import SessionContext
from core.asset_class import AssetClassConfig
from strategies.setup_fade_extreme import FadeExtremeSetup


CRYPTO = AssetClassConfig(
    name="crypto", timezone="UTC",
    session_open_local="00:00", session_close_local="23:59",
    opening_blackout_min=15, bar_timeframe="5Min",
    slippage_bps=5.0, commission_per_share=0.0, commission_bps=25.0,
)


def _bar(ts, o, h, l, c, v=100):
    return Bar(symbol="X", ts=ts, open=o, high=h, low=l, close=c, volume=v)


def test_balance_day_rejection_at_upper_band_emits_short():
    ctx = SessionContext(symbol="X", asset_class=CRYPTO)
    setup = FadeExtremeSetup("X", atr_mult_stop=0.75, min_in_value_bars=6,
                             scale_offsets_atr=[0.0, 0.25, 0.5],
                             scale_weights=[0.4, 0.35, 0.25])
    base = datetime(2026, 5, 14, 0, 5, tzinfo=timezone.utc)
    # 6 in-value bars to qualify the day as balance + meet min_in_value_bars
    bars = [_bar(base + timedelta(minutes=5*i), 100, 100.4, 99.6, 100) for i in range(6)]
    # rejection bar: wicks above upper band but closes back inside
    bars.append(_bar(base + timedelta(minutes=30), 100, 102, 99.8, 100.1))

    sig = None
    for b in bars:
        ctx.ingest(b)
        s = setup.check(ctx)
        if s is not None:
            sig = s
    assert sig is not None
    assert sig.side == "short"
    assert sig.target < sig.entry         # target = vwap


def test_no_fire_when_not_balance_day():
    ctx = SessionContext(symbol="X", asset_class=CRYPTO)
    ctx.avg_range_20d = 1.0
    setup = FadeExtremeSetup("X", atr_mult_stop=0.75, min_in_value_bars=6,
                             scale_offsets_atr=[0.0, 0.25, 0.5],
                             scale_weights=[0.4, 0.35, 0.25])
    base = datetime(2026, 5, 14, 0, 5, tzinfo=timezone.utc)
    # trending bars (all above vwap-progression)
    bars = []
    for i in range(8):
        c = 100 + i * 1.0
        bars.append(_bar(base + timedelta(minutes=5*i), c - 0.5, c + 0.5, c - 0.5, c))
    sig = None
    for b in bars:
        ctx.ingest(b)
        s = setup.check(ctx)
        if s is not None:
            sig = s
    assert sig is None
