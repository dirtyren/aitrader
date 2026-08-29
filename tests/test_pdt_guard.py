import pytest

from risk.pdt_guard import PDTViolation, check_pdt_headroom


def _account(**kw) -> dict:
    base = {
        "equity": "100000",
        "pattern_day_trader": False,
        "account_blocked": False,
        "trading_blocked": False,
    }
    base.update(kw)
    return base


def test_ample_equity_passes():
    check_pdt_headroom(_account(equity="100000"))


def test_equity_exactly_at_threshold_passes():
    check_pdt_headroom(_account(equity="25000"))


def test_equity_below_threshold_raises():
    with pytest.raises(PDTViolation, match="25000"):
        check_pdt_headroom(_account(equity="24999"))


def test_disabled_guard_permits_low_equity():
    """Paper accounts do not enforce PDT; the guard is config-gated."""
    check_pdt_headroom(_account(equity="1000"), enabled=False)


def test_flagged_pattern_day_trader_below_threshold_raises():
    with pytest.raises(PDTViolation):
        check_pdt_headroom(_account(equity="20000", pattern_day_trader=True))


def test_flagged_pattern_day_trader_above_threshold_passes():
    check_pdt_headroom(_account(equity="30000", pattern_day_trader=True))


def test_blocked_account_raises_even_with_ample_equity():
    with pytest.raises(PDTViolation, match="blocked"):
        check_pdt_headroom(_account(trading_blocked=True))
    with pytest.raises(PDTViolation, match="blocked"):
        check_pdt_headroom(_account(account_blocked=True))


def test_custom_threshold_respected():
    with pytest.raises(PDTViolation):
        check_pdt_headroom(_account(equity="40000"), min_equity=50_000.0)


def test_missing_equity_key_raises_rather_than_assuming_safe():
    with pytest.raises(PDTViolation, match="equity"):
        check_pdt_headroom({"pattern_day_trader": False})


def test_unparseable_equity_raises():
    with pytest.raises(PDTViolation, match="equity"):
        check_pdt_headroom(_account(equity="not-a-number"))
