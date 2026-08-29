"""Guards the two-strategy capital split on one Alpaca account.

sma_slope holds its position for days. If it takes 95% of notional, the
account's non_marginable_buying_power goes near zero, size_position returns
qty=0, and Opening Drive silently takes no trades while appearing to find no
candidates. These tests pin the split that prevents that.
"""
import yaml

from risk.sizing import SizingConfig, size_position

SMA = "config/settings_sma_slope_equity.yaml"
OD = "config/settings_opening_drive_equity.yaml"


def _risk(path: str) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)["risk"]


def test_sma_slope_leaves_headroom():
    assert _risk(SMA)["max_notional_per_trade_pct"] == 0.60


def test_combined_notional_fits_in_the_account():
    sma = _risk(SMA)
    od = _risk(OD)
    combined = (
        sma["max_notional_per_trade_pct"] * sma["max_concurrent_positions"]
        + od["max_notional_per_trade_pct"] * od["max_concurrent_positions"]
    )
    assert combined <= 0.98, f"combined notional {combined:.2f} overcommits"


def test_opening_drive_sizes_nonzero_with_sma_slope_fully_deployed():
    """Verifies Opening Drive can commit its full notional when sma_slope is deployed.

    Two assertions together form the regression guard:

    (1) Starvation case — near-zero buying power produces qty=0. This is the
        actual failure mode the brief describes: sma_slope holds TQQQ for days
        and the broker's non_marginable_buying_power drops close to zero.
        size_position must return qty=0, not silently commit a tiny position.

    (2) Healthy case — available_cash is DERIVED from the live sma_slope config
        so this assertion fails if that config ever reverts to a value that
        squeezes Opening Drive below its full 7% notional target. It does NOT
        independently pin the config value itself; test_sma_slope_leaves_headroom
        owns that. Together they are the complete guard.

    Note: the reviewer's suggested starved value (equity * 0.05) yields qty=49
    at entry=100 (cash_cap=4900 still covers 49 shares), so a genuinely near-zero
    value (equity * 0.001 = $100) is used instead to demonstrate the mechanism.
    """
    equity = 100_000.0
    sma = _risk(SMA)
    od = _risk(OD)
    cfg = SizingConfig(
        max_risk_per_trade=od["max_risk_per_trade"],
        max_notional_per_trade_pct=od["max_notional_per_trade_pct"],
        allow_fractional=False,
    )

    # (1) Starvation case: near-zero buying power (broker reports ~$100 of
    # non_marginable_buying_power after sma_slope's 3x-leveraged TQQQ position
    # settles). cash_cap = $100 * 0.98 = $98 < entry=$100 → qty = floor(0.98) = 0.
    near_zero_cash = equity * 0.001   # $100 on a $100k account
    starved_qty, _ = size_position(
        equity=equity, entry=100.0, stop=98.5,
        cfg=cfg, available_cash=near_zero_cash,
    )
    assert starved_qty == 0, "starvation case: expected qty=0 with near-zero buying power"

    # (2) Healthy case: cash derived from the live sma_slope allocation.
    # The lower bound asserts the full notional cap is achievable — i.e., cash
    # is NOT the binding constraint. This fails if sma_slope reverts to 0.95:
    #   available_cash = equity * 0.05 = 5000 → notional = 4900 < 7000.
    available_cash = equity * (1.0 - sma["max_notional_per_trade_pct"])
    qty, notional = size_position(
        equity=equity, entry=100.0, stop=98.5,
        cfg=cfg, available_cash=available_cash,
    )
    target_notional = equity * od["max_notional_per_trade_pct"]
    assert qty > 0, "Opening Drive sized to zero — the starvation bug"
    assert notional >= target_notional - 1e-6, (
        f"cash constrained: notional {notional:.0f} < target {target_notional:.0f} "
        f"— sma_slope allocation ({sma['max_notional_per_trade_pct']}) leaves too little headroom"
    )
    assert notional <= target_notional + 1e-6


def test_notional_cap_binds_before_the_risk_cap():
    """Documents which limit is load-bearing: with a 1.5% stop, risk-based
    sizing would ask for ~33% of equity, so the 7% notional cap governs."""
    od = _risk(OD)
    equity = 100_000.0
    cfg = SizingConfig(
        max_risk_per_trade=od["max_risk_per_trade"],
        max_notional_per_trade_pct=od["max_notional_per_trade_pct"],
        allow_fractional=False,
    )
    _, notional = size_position(equity, 100.0, 98.5, cfg,
                                available_cash=equity)
    assert notional <= equity * od["max_notional_per_trade_pct"] + 1e-6
    risk_only_notional = (equity * od["max_risk_per_trade"] / 1.5) * 100.0
    assert risk_only_notional > notional
