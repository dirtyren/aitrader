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
    """The regression this whole split exists to prevent."""
    equity = 100_000.0
    available_cash = equity * 0.40          # sma_slope holding 60%
    od = _risk(OD)
    qty, notional = size_position(
        equity=equity, entry=100.0, stop=98.5,
        cfg=SizingConfig(
            max_risk_per_trade=od["max_risk_per_trade"],
            max_notional_per_trade_pct=od["max_notional_per_trade_pct"],
            allow_fractional=False,
        ),
        available_cash=available_cash,
    )
    assert qty > 0, "Opening Drive sized to zero — the starvation bug"
    assert notional <= equity * od["max_notional_per_trade_pct"] + 1e-6


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
