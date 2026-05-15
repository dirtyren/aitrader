from datetime import datetime, timezone, timedelta
from core.bar import Bar
from core.session import SessionContext
from core.asset_class import AssetClassConfig
from strategies.setup_price_discovery import PriceDiscoverySetup


CRYPTO = AssetClassConfig(
    name="crypto", timezone="UTC",
    session_open_local="00:00", session_close_local="23:59",
    opening_blackout_min=15, bar_timeframe="5Min",
    slippage_bps=5.0, commission_per_share=0.0, commission_bps=25.0,
)


def _bar(ts, o, h, l, c, v=100):
    return Bar(symbol="X", ts=ts, open=o, high=h, low=l, close=c, volume=v)


def _drive(ctx, setup, bars):
    sig = None
    for b in bars:
        ctx.ingest(b)
        s = setup.check(ctx)
        if s is not None:
            sig = s
    return sig


def test_breakout_then_backtest_emits_long_signal():
    ctx = SessionContext(symbol="X", asset_class=CRYPTO)
    setup = PriceDiscoverySetup("X", atr_mult_stop=1.0, target_R=1.5,
                                arm_window_bars=6, accept_n=2,
                                accept_distance_atr=0.0)  # 0 to keep test simple
    base = datetime(2026, 5, 14, 0, 5, tzinfo=timezone.utc)

    # Build a tight balance: 6 bars at ~100, vol moderate
    bars = [_bar(base + timedelta(minutes=5*i), 100, 100.5, 99.5, 100, v=100) for i in range(6)]
    # Breakout: 2 strong closes above the upper band
    bars += [_bar(base + timedelta(minutes=5*(6+i)), 101, 102, 100.8, 101.5, v=200) for i in range(2)]
    # Bar 8: retrace (ACCEPTED -> ARMED)
    bars.append(_bar(base + timedelta(minutes=5*8), 101.5, 102, 100.6, 101.2, v=150))
    # Bar 9: FILL — wicks into band, closes BULLISH back above
    bars.append(_bar(base + timedelta(minutes=5*9), 101.0, 102.0, 100.4, 101.6, v=180))

    sig = _drive(ctx, setup, bars)
    assert sig is not None
    assert sig.side == "long"
    assert sig.entry < sig.target
    assert sig.stop < sig.entry


def test_no_signal_when_breakout_fails():
    ctx = SessionContext(symbol="X", asset_class=CRYPTO)
    setup = PriceDiscoverySetup("X", atr_mult_stop=1.0, target_R=1.5,
                                arm_window_bars=6, accept_n=2,
                                accept_distance_atr=0.0)
    base = datetime(2026, 5, 14, 0, 5, tzinfo=timezone.utc)
    bars = [_bar(base + timedelta(minutes=5*i), 100, 100.5, 99.5, 100) for i in range(10)]
    sig = _drive(ctx, setup, bars)
    assert sig is None
