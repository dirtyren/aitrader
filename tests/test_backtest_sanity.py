"""Three sanity tests the backtest engine MUST pass before being trusted (spec §7)."""
from datetime import datetime, timedelta, timezone

import pandas as pd

from backtest.intraday_replay import IntradayReplay
from core.asset_class import AssetClassConfig
from core.bar import Bar


CRYPTO = AssetClassConfig(
    name="crypto", timezone="UTC",
    session_open_local="00:00", session_close_local="23:59",
    opening_blackout_min=0, bar_timeframe="5Min",
    slippage_bps=0.0, commission_per_share=0.0, commission_bps=0.0,
)


def _flat_bars(symbol: str, n: int):
    base = datetime(2026, 5, 14, 0, 5, tzinfo=timezone.utc)
    return [Bar(symbol=symbol, ts=base + timedelta(minutes=5 * i),
                open=100.0, high=100.0, low=100.0, close=100.0, volume=100)
            for i in range(n)]


def _cfg(*, disabled: bool):
    enabled = not disabled
    return {
        "setups": {
            "price_discovery": {"enabled": enabled, "atr_mult_stop": 1.0, "target_R": 1.5,
                                "arm_window_bars": 6, "cooldown_bars": 12},
            "fade_extreme": {"enabled": enabled, "atr_mult_stop": 0.75,
                             "scale_offsets_atr": [0.0], "scale_weights": [1.0],
                             "cooldown_bars": 12},
            "return_to_value": {"enabled": enabled, "atr_mult_stop": 1.0,
                                "arm_window_bars": 6, "cooldown_bars": 12},
            "vwap_bounce": {"enabled": enabled, "atr_mult_stop": 1.25, "target_R": 2.0,
                            "arm_window_bars": 4, "cooldown_bars": 8},
        },
        "risk": {
            "max_risk_per_trade": 0.005, "max_notional_per_trade_pct": 0.20,
            "max_concurrent_positions": 4, "max_daily_risk_open": 0.02,
            "consecutive_loss_limit": 2, "loss_filter_scope": "per_symbol",
            "circuit_breaker": {"daily_loss_limit_1": 0.015, "daily_loss_limit_2": 0.025,
                                "drawdown_limit": 0.05},
        },
        "filters": {"opening_blackout_min": 0, "volume_deficit_pct": 0.30},
        "position_management": {"max_hold_bars": 12, "breakeven_at_R": 1.0},
        "scheduler": {"bar_timeframe": "5Min"},
    }


def test_flat_bars_with_setups_enabled_yield_zero_trades():
    """A perfectly flat tape leaves nothing for the setups to fire on."""
    bars = {"BTC/USD": _flat_bars("BTC/USD", 50)}
    res = IntradayReplay([("BTC/USD", "crypto")], {"crypto": CRYPTO},
                         bars, 100_000.0, _cfg(disabled=False)).run()
    assert res.metrics["trades"] == 0
    assert abs(res.equity_curve.iloc[-1] - 100_000.0) < 1e-9


def test_two_runs_with_identical_inputs_match_exactly():
    bars = {"BTC/USD": _flat_bars("BTC/USD", 80)}
    a = IntradayReplay([("BTC/USD", "crypto")], {"crypto": CRYPTO},
                       bars, 100_000.0, _cfg(disabled=True)).run()
    b = IntradayReplay([("BTC/USD", "crypto")], {"crypto": CRYPTO},
                       bars, 100_000.0, _cfg(disabled=True)).run()
    pd.testing.assert_series_equal(a.equity_curve, b.equity_curve)
    assert a.metrics == b.metrics
    assert a.filter_audit == b.filter_audit


def test_replay_completes_on_realistic_synthetic_path():
    """Gate: completes without errors, regardless of profitability."""
    base = datetime(2026, 5, 14, 0, 5, tzinfo=timezone.utc)
    bars = []
    for i in range(120):
        c = 100 + (i % 17) - 8           # wedged random-walk-ish path
        bars.append(Bar(symbol="BTC/USD", ts=base + timedelta(minutes=5 * i),
                        open=c - 0.2, high=c + 0.4, low=c - 0.4, close=c, volume=100))
    res = IntradayReplay([("BTC/USD", "crypto")], {"crypto": CRYPTO},
                         {"BTC/USD": bars}, 100_000.0, _cfg(disabled=False)).run()
    assert res.equity_curve is not None
    assert "trades" in res.metrics
    assert "win_rate" in res.metrics
    # Equity curve covers initial point + every bar
    assert len(res.equity_curve) == len(bars) + 1
