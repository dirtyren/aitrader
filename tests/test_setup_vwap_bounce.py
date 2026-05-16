from datetime import datetime, timezone, timedelta
from core.bar import Bar
from core.session import SessionContext
from core.asset_class import AssetClassConfig
from strategies.setup_vwap_bounce import VWAPBounceSetup


CRYPTO = AssetClassConfig(
    name="crypto", timezone="UTC",
    session_open_local="00:00", session_close_local="23:59",
    opening_blackout_min=15, bar_timeframe="5Min",
    slippage_bps=5.0, commission_per_share=0.0, commission_bps=25.0,
)


def _bar(ts, o, h, l, c, v=100):
    return Bar(symbol="X", ts=ts, open=o, high=h, low=l, close=c, volume=v)


def test_uptrend_sub_vwap_trap_then_reclaim_pullback_emits_long():
    ctx = SessionContext(symbol="X", asset_class=CRYPTO)
    ctx.avg_range_20d = 2.0
    setup = VWAPBounceSetup("X", atr_mult_stop=1.25, target_R=2.0,
                            arm_window_bars=4, trend_majority=0.7,
                            trend_range_mult=1.5)
    base = datetime(2026, 5, 14, 0, 5, tzinfo=timezone.utc)
    bars = []
    # Strong uptrend: 12 bars marching up, range >> avg
    for i in range(12):
        c = 100 + i * 1.0
        bars.append(_bar(base + timedelta(minutes=5*i), c - 0.5, c + 0.5, c - 0.5, c))
    # Sub-VWAP trap: dip below current VWAP
    bars.append(_bar(base + timedelta(minutes=5*12), 110, 110.2, 99.0, 99.2))
    # Reclaim: bar closes back above VWAP
    bars.append(_bar(base + timedelta(minutes=5*13), 99.2, 112.0, 99.0, 111.0))
    # Pullback to vwap → fill
    bars.append(_bar(base + timedelta(minutes=5*14), 111.0, 111.5, 105.0, 110.5))
    # Add a closer pullback that's more clearly within retrace_proximity_atr of vwap:
    bars.append(_bar(base + timedelta(minutes=5*15), 110.0, 110.5, 106.0, 109.5))

    sig = None
    for b in bars:
        ctx.ingest(b)
        s = setup.check(ctx)
        if s is not None:
            sig = s
    assert sig is not None
    assert sig.side == "long"
    assert sig.target > sig.entry
