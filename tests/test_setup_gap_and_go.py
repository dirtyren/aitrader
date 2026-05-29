"""Unit tests for the Gap-and-Go pre-market breakout setup."""
from __future__ import annotations
from datetime import datetime, timedelta, timezone

import pytest

from core.asset_class import AssetClassConfig
from core.bar import Bar
from core.session import SessionContext
from strategies.setup_gap_and_go import GapAndGoSetup


_EQ = AssetClassConfig(
    name="equity", timezone="America/New_York",
    session_open_local="04:00", session_close_local="20:00",
    opening_blackout_min=0, bar_timeframe="1Min",
    slippage_bps=2.0, commission_per_share=0.0, commission_bps=0.0,
)

# 08:30 ET on 2026-05-29 in UTC (EDT = UTC-4 → 12:30 UTC).
_CUT_UTC = datetime(2026, 5, 29, 12, 30, tzinfo=timezone.utc)
_DEADLINE_UTC = datetime(2026, 5, 29, 13, 30, tzinfo=timezone.utc)  # 09:30 ET


def _bar(ts, o, h, l, c, v=1000.0, symbol="AAPL") -> Bar:
    return Bar(symbol=symbol, ts=ts, open=o, high=h, low=l, close=c, volume=v)


def _ctx(symbol="AAPL") -> SessionContext:
    return SessionContext(symbol=symbol, asset_class=_EQ)


def _setup(**overrides) -> GapAndGoSetup:
    defaults = dict(
        symbol="AAPL",
        premarket_high=200.0,
        premarket_low=197.0,
        atr_14d=2.0,
        entry_deadline=_DEADLINE_UTC,
    )
    defaults.update(overrides)
    return GapAndGoSetup(**defaults)


def _seed_volume_priors(ctx: SessionContext, count=5, base_vol=500.0,
                       start_ts=None, price=199.0) -> datetime:
    """Seed N quiet bars (price below PMH) so the trailing volume avg exists."""
    ts = start_ts or _CUT_UTC
    for i in range(count):
        b = _bar(ts + timedelta(minutes=i),
                 o=price, h=price + 0.1, l=price - 0.1, c=price, v=base_vol)
        ctx.ingest(b)
    return ts + timedelta(minutes=count)


# ---------------------------------------------------------------------------
# Construction guards
# ---------------------------------------------------------------------------


def test_rejects_non_positive_atr():
    with pytest.raises(ValueError, match="atr_14d"):
        _setup(atr_14d=0.0)


def test_rejects_inverted_premarket_levels():
    with pytest.raises(ValueError, match="premarket_low"):
        _setup(premarket_high=200.0, premarket_low=200.0)


def test_rejects_naive_entry_deadline():
    with pytest.raises(ValueError, match="timezone-aware"):
        _setup(entry_deadline=datetime(2026, 5, 29, 13, 30))


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_happy_path_high_break_with_volume_confirm_emits_signal():
    ctx = _ctx()
    setup = _setup()
    next_ts = _seed_volume_priors(ctx, count=5, base_vol=500.0)

    # Trigger bar: closes 0.1 above PMH (200.0) on 2x avg volume (1000 vs 500).
    trigger = _bar(next_ts, o=199.5, h=200.05, l=199.4, c=200.05, v=1100.0)
    ctx.ingest(trigger)

    sig = setup.check(ctx)
    assert sig is not None
    assert sig.symbol == "AAPL"
    assert sig.side == "long"
    assert sig.entry == 200.05
    # stop = max(PML=197.0, entry - 2.0*ATR=200.05 - 4.0=196.05) → PML wins
    assert sig.stop == 197.0
    # target = entry + 2 * (entry - stop) = 200.05 + 2*3.05 = 206.15
    assert sig.target == pytest.approx(206.15)
    assert sig.notes["extended_hours"] is True
    assert sig.notes["style"] == "gap_continuation"
    assert sig.notes["premarket_high"] == 200.0
    assert setup.state == "FILLED"


def test_atr_cap_lifts_stop_above_premarket_low():
    ctx = _ctx()
    # PML far below: ATR cap of 1.0 * 2.0 = 2.0 → stop floor at entry - 2.0
    setup = _setup(premarket_low=180.0, atr_14d=1.0, atr_mult_stop_cap=1.0)
    next_ts = _seed_volume_priors(ctx, count=5, base_vol=500.0)

    trigger = _bar(next_ts, o=199.5, h=200.05, l=199.4, c=200.05, v=1100.0)
    ctx.ingest(trigger)

    sig = setup.check(ctx)
    assert sig is not None
    # stop = max(180.0, 200.05 - 1.0*1.0=199.05) → ATR floor wins
    assert sig.stop == pytest.approx(199.05)


def test_state_remains_filled_after_signal():
    ctx = _ctx()
    setup = _setup()
    next_ts = _seed_volume_priors(ctx, count=5, base_vol=500.0)
    ctx.ingest(_bar(next_ts, o=199.5, h=200.05, l=199.4, c=200.05, v=1100.0))
    assert setup.check(ctx) is not None
    assert setup.state == "FILLED"

    # Subsequent bars must not emit a second signal.
    ctx.ingest(_bar(next_ts + timedelta(minutes=1),
                    o=200.05, h=200.5, l=199.9, c=200.4, v=2000.0))
    assert setup.check(ctx) is None


# ---------------------------------------------------------------------------
# Rejections
# ---------------------------------------------------------------------------


def test_volume_only_fakeout_rejected():
    ctx = _ctx()
    setup = _setup(volume_confirm_mult=2.0)
    next_ts = _seed_volume_priors(ctx, count=5, base_vol=500.0)

    # Close above PMH but volume only 1.5x avg.
    trigger = _bar(next_ts, o=199.5, h=200.05, l=199.4, c=200.05, v=750.0)
    ctx.ingest(trigger)
    assert setup.check(ctx) is None
    assert setup.state == "IDLE"


def test_close_below_premarket_high_rejected_and_extends_pmh():
    """A wick-only bar (high > PMH but close <= PMH) is a fakeout: extend, skip."""
    ctx = _ctx()
    setup = _setup()
    next_ts = _seed_volume_priors(ctx, count=5, base_vol=500.0)

    trigger = _bar(next_ts, o=199.0, h=200.5, l=198.5, c=199.5, v=2000.0)
    ctx.ingest(trigger)
    assert setup.check(ctx) is None
    assert setup.premarket_high == 200.5  # extended for next bar's comparison
    assert setup.state == "IDLE"


def test_slippage_guard_rejects_wide_overshoot():
    """Close more than 0.5% above prior PMH triggers the slippage guard."""
    ctx = _ctx()
    setup = _setup(max_entry_slippage_pct=0.5)
    next_ts = _seed_volume_priors(ctx, count=5, base_vol=500.0)

    # Close 1.2% above PMH=200.0 → entry 202.4. (202.4 - 200) / 200 = 1.2%.
    trigger = _bar(next_ts, o=200.5, h=202.5, l=200.4, c=202.4, v=2000.0)
    ctx.ingest(trigger)
    assert setup.check(ctx) is None
    assert setup.state == "IDLE"
    # PMH bumps to bar.high so the next bar's comparison is realistic.
    assert setup.premarket_high == 202.5


def test_deadline_expiry():
    ctx = _ctx()
    setup = _setup(entry_deadline=_DEADLINE_UTC)
    next_ts = _seed_volume_priors(ctx, count=5, base_vol=500.0,
                                  start_ts=_DEADLINE_UTC - timedelta(minutes=5))

    # Bar at exactly the deadline → state goes EXPIRED, no signal.
    ctx.ingest(_bar(_DEADLINE_UTC, o=199.5, h=199.8, l=199.4, c=199.6, v=2000.0))
    assert setup.check(ctx) is None
    assert setup.state == "EXPIRED"


def test_premarket_high_extends_when_bar_fakes_out():
    """A wick-only breakout (high>PMH, close<=PMH) extends PMH; next bar
    must beat the NEW level."""
    ctx = _ctx()
    setup = _setup(premarket_high=200.0)
    next_ts = _seed_volume_priors(ctx, count=5, base_vol=500.0)

    # Bar 1: wicks above PMH but closes below → fakeout, PMH extends.
    ctx.ingest(_bar(next_ts, o=199.5, h=200.6, l=199.4, c=199.8, v=2000.0))
    assert setup.check(ctx) is None
    assert setup.premarket_high == 200.6
    assert setup.state == "IDLE"

    # Bar 2: closes 200.5 > original PMH (200.0) but BELOW new PMH (200.6) →
    # no trigger because we compare against the running PMH.
    ctx.ingest(_bar(next_ts + timedelta(minutes=1),
                    o=199.8, h=200.5, l=199.7, c=200.5, v=2000.0))
    assert setup.check(ctx) is None
    assert setup.state == "IDLE"


def test_no_signal_before_priors_collected():
    ctx = _ctx()
    setup = _setup()
    # Single bar — no priors → trailing volume avg cannot be computed.
    ctx.ingest(_bar(_CUT_UTC, o=199.5, h=200.05, l=199.4, c=200.05, v=10000.0))
    assert setup.check(ctx) is None
    assert setup.state == "IDLE"
