import pytest
from risk.sizing import size_position, SizingConfig


def test_basic_sizing():
    # Use a notional cap large enough that the risk-per-trade limit binds, not the cap.
    cfg = SizingConfig(max_risk_per_trade=0.005, max_notional_per_trade_pct=0.60)
    qty, notional = size_position(equity=100000, entry=100, stop=99, cfg=cfg)
    # risk = 500; per-share risk = 1; qty = 500
    assert qty == 500
    assert notional == 500 * 100


def test_notional_cap_clamps_qty():
    cfg = SizingConfig(max_risk_per_trade=0.005, max_notional_per_trade_pct=0.10)
    qty, notional = size_position(equity=100000, entry=100, stop=99, cfg=cfg)
    # risk-based qty would be 500, notional 50k. cap = 10% x 100k = 10k, qty floor(10k/100) = 100
    assert qty == 100
    assert notional == 100 * 100


def test_zero_stop_distance_raises():
    cfg = SizingConfig(max_risk_per_trade=0.005, max_notional_per_trade_pct=0.20)
    with pytest.raises(ValueError):
        size_position(equity=100000, entry=100, stop=100, cfg=cfg)


def test_available_cash_caps_qty_below_notional_cap():
    # equity=100k → 20% notional cap = 20k, but only 18k cash available.
    # With 2% buffer: cash_cap = 18000 * 0.98 = 17640 → qty = floor(17640/100) = 176
    cfg = SizingConfig(max_risk_per_trade=0.005, max_notional_per_trade_pct=0.20)
    qty, notional = size_position(equity=100000, entry=100, stop=99, cfg=cfg,
                                  available_cash=18000)
    assert qty == 176
    assert notional == 176 * 100


def test_available_cash_none_preserves_legacy_behavior():
    cfg = SizingConfig(max_risk_per_trade=0.005, max_notional_per_trade_pct=0.10)
    qty_a, _ = size_position(equity=100000, entry=100, stop=99, cfg=cfg)
    qty_b, _ = size_position(equity=100000, entry=100, stop=99, cfg=cfg, available_cash=None)
    assert qty_a == qty_b == 100


def test_available_cash_does_not_inflate_above_notional_cap():
    # Plenty of cash — notional cap should still bind.
    cfg = SizingConfig(max_risk_per_trade=0.005, max_notional_per_trade_pct=0.10)
    qty, _ = size_position(equity=100000, entry=100, stop=99, cfg=cfg,
                           available_cash=1_000_000)
    assert qty == 100


def test_fractional_qty_supported_for_crypto():
    # Use a notional cap large enough that the risk-per-trade limit binds, not the cap.
    cfg = SizingConfig(max_risk_per_trade=0.01, max_notional_per_trade_pct=2.0,
                       allow_fractional=True)
    qty, _ = size_position(equity=10000, entry=50000, stop=49500, cfg=cfg)
    # risk = 100; per-share = 500; qty = 0.2
    assert abs(qty - 0.2) < 1e-9
