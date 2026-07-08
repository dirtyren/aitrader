from datetime import datetime, timedelta, timezone

from backtest.intraday_replay import IntradayReplay
from core.asset_class import AssetClassConfig
from core.bar import Bar


CRYPTO = AssetClassConfig(
    name="crypto", timezone="UTC",
    session_open_local="00:00", session_close_local="23:59",
    opening_blackout_min=0, bar_timeframe="5Min",
    slippage_bps=5.0, commission_per_share=0.0, commission_bps=0.0,
)


def _bars(symbol, n, start_price=100.0, base=None):
    base = base or datetime(2026, 5, 14, 0, 5, tzinfo=timezone.utc)
    out = []
    for i in range(n):
        c = start_price + i * 0.1
        out.append(Bar(symbol=symbol, ts=base + timedelta(minutes=5 * i),
                       open=c - 0.05, high=c + 0.05, low=c - 0.05, close=c, volume=100))
    return out


def _disabled_setups_config():
    return {
        "setups": {
            "price_discovery": {"enabled": False, "atr_mult_stop": 1.0, "target_R": 1.5,
                                "arm_window_bars": 6, "cooldown_bars": 12},
            "fade_extreme": {"enabled": False, "atr_mult_stop": 0.75,
                             "scale_offsets_atr": [0.0], "scale_weights": [1.0],
                             "cooldown_bars": 12},
            "return_to_value": {"enabled": False, "atr_mult_stop": 1.0,
                                "arm_window_bars": 6, "cooldown_bars": 12},
            "vwap_bounce": {"enabled": False, "atr_mult_stop": 1.25, "target_R": 2.0,
                            "arm_window_bars": 4, "cooldown_bars": 8},
        },
        "risk": {
            "max_risk_per_trade": 0.005, "max_notional_per_trade_pct": 0.20,
            "max_concurrent_positions": 4, "max_daily_risk_open": 0.02,
            "consecutive_loss_limit": 2, "loss_filter_scope": "per_symbol",
            },
        "filters": {"opening_blackout_min": 0, "volume_deficit_pct": 0.30},
        "position_management": {"max_hold_bars": 12, "breakeven_at_R": 1.0},
        "scheduler": {"bar_timeframe": "5Min"},
    }


def test_no_signal_universe_yields_flat_equity():
    bars = {"BTC/USD": _bars("BTC/USD", 50, start_price=100)}
    replay = IntradayReplay(
        symbols=[("BTC/USD", "crypto")],
        asset_class_configs={"crypto": CRYPTO},
        bars=bars,
        initial_equity=100_000.0,
        config=_disabled_setups_config(),
    )
    result = replay.run()
    assert result.metrics["trades"] == 0
    assert abs(result.equity_curve.iloc[-1] - 100_000.0) < 1e-9


def test_idempotency():
    bars = {"BTC/USD": _bars("BTC/USD", 100, start_price=50_000)}
    cfg = _disabled_setups_config()
    a = IntradayReplay([("BTC/USD", "crypto")], {"crypto": CRYPTO}, bars, 100_000.0, cfg).run()
    b = IntradayReplay([("BTC/USD", "crypto")], {"crypto": CRYPTO}, bars, 100_000.0, cfg).run()
    assert list(a.equity_curve) == list(b.equity_curve)
    assert a.metrics == b.metrics


def test_metrics_shape_when_no_trades():
    bars = {"BTC/USD": _bars("BTC/USD", 5)}
    result = IntradayReplay([("BTC/USD", "crypto")], {"crypto": CRYPTO},
                            bars, 100_000.0, _disabled_setups_config()).run()
    for key in ("trades", "final_equity", "total_return", "win_rate", "avg_R"):
        assert key in result.metrics
    assert result.trades.empty
    assert result.per_setup == {}
    assert result.per_symbol == {}


def test_chronological_timeline_across_symbols():
    """Bars for two symbols are interleaved by ts so context ingest is monotonic."""
    base = datetime(2026, 5, 14, 0, 5, tzinfo=timezone.utc)
    bars = {
        "BTC/USD": [Bar(symbol="BTC/USD", ts=base + timedelta(minutes=5 * i),
                        open=100, high=100.5, low=99.5, close=100, volume=100)
                    for i in range(3)],
        "ETH/USD": [Bar(symbol="ETH/USD", ts=base + timedelta(minutes=5 * i, seconds=1),
                        open=200, high=200.5, low=199.5, close=200, volume=100)
                    for i in range(3)],
    }
    result = IntradayReplay(
        [("BTC/USD", "crypto"), ("ETH/USD", "crypto")],
        {"crypto": CRYPTO}, bars, 100_000.0, _disabled_setups_config(),
    ).run()
    # Equity curve covers initial + 6 bar points
    assert len(result.equity_curve) == 7
