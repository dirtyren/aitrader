from datetime import datetime, timezone, timedelta
from core.asset_class import AssetClassConfig, session_start_for


EQUITY = AssetClassConfig(
    name="equity",
    timezone="America/New_York",
    session_open_local="09:30",
    session_close_local="16:00",
    opening_blackout_min=15,
    bar_timeframe="5Min",
    slippage_bps=2.0,
    commission_per_share=0.0,
    commission_bps=0.0,
)

CRYPTO = AssetClassConfig(
    name="crypto",
    timezone="UTC",
    session_open_local="00:00",
    session_close_local="23:59",
    opening_blackout_min=15,
    bar_timeframe="5Min",
    slippage_bps=5.0,
    commission_per_share=0.0,
    commission_bps=25.0,
)


def test_equity_session_start_today_in_utc():
    # 14 May 2026 18:00 UTC = 14:00 ET (DST). Same calendar day, after open.
    now = datetime(2026, 5, 14, 18, 0, tzinfo=timezone.utc)
    start = session_start_for(now, EQUITY)
    # 9:30 ET = 13:30 UTC during DST
    assert start == datetime(2026, 5, 14, 13, 30, tzinfo=timezone.utc)


def test_equity_session_before_open_falls_back_to_yesterday():
    # 14 May 2026 12:00 UTC = 08:00 ET — before the 09:30 ET open today
    now = datetime(2026, 5, 14, 12, 0, tzinfo=timezone.utc)
    start = session_start_for(now, EQUITY)
    # Should use 13 May session
    assert start == datetime(2026, 5, 13, 13, 30, tzinfo=timezone.utc)


def test_crypto_session_start_is_utc_midnight():
    now = datetime(2026, 5, 14, 18, 0, tzinfo=timezone.utc)
    start = session_start_for(now, CRYPTO)
    assert start == datetime(2026, 5, 14, 0, 0, tzinfo=timezone.utc)
