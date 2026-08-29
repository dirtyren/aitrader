# tests/test_opening_drive_setup.py
from datetime import datetime, timedelta, timezone

import pytest

from core.asset_class import AssetClassConfig
from core.bar import Bar
from core.session import SessionContext
from strategies.setup_opening_drive import OpeningDriveSetup

OR_HIGH = 105.0
OR_LOW = 99.0
ATR = 4.0
AVG_MIN_VOL = 1_000.0
CUT = datetime(2026, 8, 28, 14, 0, tzinfo=timezone.utc)      # 10:00 NY
DEADLINE = datetime(2026, 8, 28, 15, 0, tzinfo=timezone.utc)  # 11:00 NY

EQUITY_AC = AssetClassConfig(
    name="equity", timezone="America/New_York",
    session_open_local="09:30", session_close_local="16:00",
    opening_blackout_min=0, bar_timeframe="1Min",
    slippage_bps=2.0, commission_per_share=0.0, commission_bps=0.0,
)


def _bar(minute: int, o, h, l, c, v) -> Bar:
    return Bar(symbol="TEST", ts=CUT + timedelta(minutes=minute),
               open=o, high=h, low=l, close=c, volume=v)


def _setup(**kw) -> OpeningDriveSetup:
    params = dict(
        symbol="TEST", or_high=OR_HIGH, or_low=OR_LOW, atr_14d=ATR,
        avg_minute_volume=AVG_MIN_VOL, entry_deadline=DEADLINE,
        volume_confirm_mult=2.0, target_R=2.0,
        min_stop_atr_frac=0.15, atr_mult_stop_cap=2.0,
    )
    params.update(kw)
    return OpeningDriveSetup(**params)


def _ctx(bars: list[Bar]) -> SessionContext:
    ctx = SessionContext(symbol="TEST", asset_class=EQUITY_AC)
    for b in bars:
        ctx.ingest(b)
    return ctx


def _feed(setup: OpeningDriveSetup, bars: list[Bar]):
    """Ingest bars one at a time, returning the first signal produced."""
    ctx = SessionContext(symbol="TEST", asset_class=EQUITY_AC)
    for b in bars:
        ctx.ingest(b)
        sig = setup.check(ctx)
        if sig is not None:
            return sig
    return None


# Low-VWAP seed bar so ctx.vwap stays well under the trigger price.
SEED = _bar(0, 100.0, 100.5, 99.0, 100.0, 1_000)
# Reclaim bar: closes above OR_HIGH, volume 3x the minute average.
RECLAIM = _bar(1, 104.0, 106.0, 103.0, 105.5, 3_000)


def test_starts_armed():
    assert _setup().state == "ARMED"


def test_no_signal_while_close_below_or_high():
    s = _setup()
    assert _feed(s, [SEED, _bar(1, 104.0, 104.9, 103.0, 104.5, 5_000)]) is None
    assert s.state == "ARMED"


def test_fires_on_reclaim_with_volume_and_above_vwap():
    s = _setup()
    sig = _feed(s, [SEED, RECLAIM])
    assert sig is not None
    assert sig.setup == "opening_drive"
    assert sig.symbol == "TEST"
    assert sig.side == "long"
    assert sig.entry == 105.5
    assert sig.level == OR_HIGH
    assert sig.atr == ATR
    assert s.state == "FILLED"


def test_no_signal_without_volume_confirmation():
    s = _setup()
    weak = _bar(1, 104.0, 106.0, 103.0, 105.5, 1_999)   # < 2x 1000
    assert _feed(s, [SEED, weak]) is None
    assert s.state == "ARMED"


def test_volume_confirmation_boundary_is_inclusive():
    s = _setup()
    exact = _bar(1, 104.0, 106.0, 103.0, 105.5, 2_000)
    assert _feed(s, [SEED, exact]) is not None


def test_no_signal_when_close_at_or_below_vwap():
    """A high-priced, high-volume seed pushes VWAP above the reclaim close."""
    s = _setup()
    rich_seed = _bar(0, 120.0, 121.0, 119.0, 120.0, 500_000)
    assert _feed(s, [rich_seed, RECLAIM]) is None
    assert s.state == "ARMED"


def test_stop_sits_at_the_running_low():
    s = _setup()
    dip = _bar(1, 104.0, 104.5, 101.0, 104.0, 1_000)     # low 101, no trigger
    reclaim = _bar(2, 104.0, 106.0, 103.5, 105.5, 3_000)
    sig = _feed(s, [SEED, dip, reclaim])
    assert sig is not None
    assert sig.stop == pytest.approx(99.0)   # SEED's low is the running low
    assert sig.entry - sig.stop == pytest.approx(6.5)


def test_stop_floored_at_min_stop_atr_frac():
    """A trigger bar that is its own low would give a near-zero stop."""
    s = _setup(min_stop_atr_frac=0.5)        # floor = 0.5 * 4.0 = 2.0
    # NOTE: brief had h=105.6, l=105.4 which makes typical_price = close = 105.5,
    # so VWAP == close and the VWAP gate rejects the signal.  Minimal fix: lower h
    # to 105.5 and l to 105.3 so typical = 105.433 < close = 105.5; the stop
    # calculation is unchanged (risk = 0.2, floored to 2.0, stop = 103.5).
    tight = _bar(0, 105.4, 105.5, 105.3, 105.5, 3_000)
    sig = _feed(s, [tight])
    assert sig is not None
    assert sig.stop == pytest.approx(105.5 - 2.0)


def test_rejects_trigger_when_structural_stop_exceeds_cap():
    """Risk beyond atr_mult_stop_cap * ATR is rejected, never clamped."""
    s = _setup(atr_mult_stop_cap=1.0)        # cap = 4.0
    deep = _bar(0, 100.0, 100.5, 95.0, 100.0, 1_000)     # low 95
    reclaim = _bar(1, 104.0, 106.0, 103.0, 105.5, 3_000)  # risk 10.5 > 4.0
    assert _feed(s, [deep, reclaim]) is None
    assert s.state == "ARMED"


def test_target_is_target_R_multiples_of_risk():
    s = _setup(target_R=3.0)
    sig = _feed(s, [SEED, RECLAIM])
    risk = sig.entry - sig.stop
    assert sig.target == pytest.approx(sig.entry + 3.0 * risk)


def test_expires_at_entry_deadline():
    s = _setup()
    late = Bar(symbol="TEST", ts=DEADLINE, open=104.0, high=106.0,
               low=103.0, close=105.5, volume=9_000)
    assert _feed(s, [SEED, late]) is None
    assert s.state == "EXPIRED"


def test_expired_setup_ignores_later_bars():
    s = _setup()
    late = Bar(symbol="TEST", ts=DEADLINE, open=104.0, high=106.0,
               low=103.0, close=105.5, volume=9_000)
    _feed(s, [SEED, late])
    assert s.state == "EXPIRED"
    assert s.check(_ctx([SEED, RECLAIM])) is None


def test_fires_only_once():
    s = _setup()
    ctx = SessionContext(symbol="TEST", asset_class=EQUITY_AC)
    for b in [SEED, RECLAIM]:
        ctx.ingest(b)
        s.check(ctx)
    again = _bar(2, 105.5, 107.0, 105.0, 106.5, 9_000)
    ctx.ingest(again)
    assert s.check(ctx) is None


def test_empty_context_is_safe():
    s = _setup()
    assert s.check(SessionContext(symbol="TEST", asset_class=EQUITY_AC)) is None


def test_notes_carry_trigger_diagnostics():
    s = _setup()
    sig = _feed(s, [SEED, RECLAIM])
    assert sig.notes["style"] == "or_high_reclaim"
    assert sig.notes["or_high"] == OR_HIGH
    assert sig.notes["or_low"] == OR_LOW
    assert sig.notes["structural_low"] == pytest.approx(99.0)
    assert sig.notes["stop_floored"] is False


def test_reset_returns_to_armed():
    s = _setup()
    _feed(s, [SEED, RECLAIM])
    assert s.state == "FILLED"
    s.reset()
    assert s.state == "ARMED"
    assert _feed(s, [SEED, RECLAIM]) is not None
