from datetime import datetime, timezone, timedelta
from core.bar import Bar
from core.session import SessionContext
from core.asset_class import AssetClassConfig
from strategies.setup_return_to_value import ReturnToValueSetup


CRYPTO = AssetClassConfig(
    name="crypto", timezone="UTC",
    session_open_local="00:00", session_close_local="23:59",
    opening_blackout_min=15, bar_timeframe="5Min",
    slippage_bps=5.0, commission_per_share=0.0, commission_bps=25.0,
)


def _bar(ts, o, h, l, c, v=100):
    return Bar(symbol="X", ts=ts, open=o, high=h, low=l, close=c, volume=v)


def test_failed_discovery_then_reentry_emits_short_signal():
    ctx = SessionContext(symbol="X", asset_class=CRYPTO)
    setup = ReturnToValueSetup("X", atr_mult_stop=1.0, arm_window_bars=6,
                                accept_n=2, accept_distance_atr=0.0)
    base = datetime(2026, 5, 14, 0, 5, tzinfo=timezone.utc)
    bars = [_bar(base + timedelta(minutes=5*i), 100, 100.5, 99.5, 100) for i in range(6)]
    # Discovery up (close above upper band)
    bars += [_bar(base + timedelta(minutes=5*(6+i)), 101, 102, 100.8, 101.5) for i in range(2)]
    # Re-entry inside value
    bars += [_bar(base + timedelta(minutes=5*(8+i)), 100.5, 100.7, 100.0, 100.2) for i in range(2)]
    # Retest the upper band from below
    bars.append(_bar(base + timedelta(minutes=5*10), 100.4, 101.0, 100.0, 100.3))
    # Bar 11: FILL — wicks up to/through level, closes back inside (short fill)
    bars.append(_bar(base + timedelta(minutes=5*11), 100.5, 101.5, 100.0, 100.4))

    sig = None
    for b in bars:
        ctx.ingest(b)
        s = setup.check(ctx)
        if s is not None:
            sig = s
    assert sig is not None
    assert sig.side == "short"
    assert sig.target < sig.entry           # target = vwap, below entry for shorts
