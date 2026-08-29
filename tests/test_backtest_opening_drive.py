"""Offline tests for the Opening Drive backtest harness.

No network, no credentials, no MySQL. Every bar is hand-constructed so the
expected R of each trade can be derived by arithmetic that is written out in
the test itself.

The numbers all descend from one synthetic session, assembled by the helpers
below (``opening_range`` / ``entry_window`` / ``managed_bars``) and reused, so
the arithmetic only has to be established once:

    baseline        atr_14d 2.00, avg_or_volume_20d 10_000,
                    avg_daily_volume_20d 500_000
    prev close      100.00                       (SPY 500.00 -> 500.50)
    opening range   29 bars  o 100.50 h 100.60 l 100.40 c 100.55 v 1_000
                    1  bar   o 100.55 h 102.00 l 100.55 c 101.90 v 1_000
      or_high 102.00  or_low 100.40  or_close 101.90  or_volume 30_000
      rvol_or       30_000 / 10_000            = 3.00   >= 2.0   pass
      disp_atr      (101.90 - 100.00) / 2.00   = 0.95   >= 0.5   pass
      or_width_atr  (102.00 - 100.40) / 2.00   = 0.80   in band  pass
      clv           (101.90 - 100.40) / 1.60   = 0.9375 >= 0.6   pass
      rs_atr        (0.019 - 0.001) / (2/100)  = 0.90   >  0     pass
      score         3.00 * 0.90                = 2.70

    trigger 10:05   o 102.00 h 102.30 l 101.80 c 102.20 v 3_000
      close 102.20 > or_high 102.00, volume 3_000 >= 2.0 * (30_000/30)
      structural low across the entry window = 101.00
      signal entry 102.20  stop 101.00  R 1.20  target 102.20 + 2*1.20 = 104.60

    fill            entry 102.20 * (1 + 2bps) = 102.220440
      initial risk  102.220440 - 101.00       =   1.220440
      qty           notional cap 7% of 100_000 = 7_000; 7_000 / 102.20
                    = 68.4931 -> floor = 68 shares
"""
from __future__ import annotations

import copy
from datetime import date, datetime, timedelta

import pytest
import yaml

from core.bar import Bar
from scripts.backtest_opening_drive import (
    CachedBarSource, OpeningDriveBacktest, apply_overrides, ny_dt,
    point_in_time_baselines, summarize,
)
from strategies.opening_drive_scanner import OpeningDriveBaseline

CONFIG_PATH = "config/settings_opening_drive_equity.yaml"

DAY = date(2026, 3, 10)          # a Tuesday
PREV = date(2026, 3, 9)

ATR = 2.0
PREV_CLOSE = 100.0
AVG_OR_VOL = 10_000.0
EQUITY = 100_000.0

# Derived once (see module docstring) so the tests can assert on them.
ENTRY_SIGNAL = 102.20
STOP = 101.00
TARGET = 104.60
SLIP = 2 / 10_000.0
ENTRY_FILL = ENTRY_SIGNAL * (1 + SLIP)          # 102.220440
INITIAL_RISK = ENTRY_FILL - STOP                # 1.220440
QTY = 68


# ─────────────────────────────────────────────────────────────────────────
# Bar helpers — every Bar goes through core.bar.Bar, which enforces the
# OHLC invariants. mk_bar exists so a typo in a fixture fails loudly.
# ─────────────────────────────────────────────────────────────────────────


def mk_bar(symbol: str, ts: datetime, o: float, h: float, l: float,
           c: float, v: float) -> Bar:
    bar = Bar(symbol=symbol, ts=ts, open=o, high=h, low=l, close=c, volume=v)
    # Belt and braces: Bar.__post_init__ raises, and we re-assert here so the
    # contract is visible at the call site too.
    assert bar.low <= min(bar.open, bar.close, bar.high)
    assert bar.high >= max(bar.open, bar.close, bar.low)
    return bar


def test_bar_construction_rejects_impossible_ohlc():
    ts = ny_dt(DAY, 9, 30)
    with pytest.raises(ValueError):
        mk_bar("BAD", ts, o=100.0, h=99.0, l=98.0, c=98.5, v=1.0)
    with pytest.raises(ValueError):
        mk_bar("BAD", ts, o=100.0, h=101.0, l=100.5, c=100.2, v=1.0)


class FakeSource:
    """``get_bars_multi``-compatible fixture source.

    Slices with an INCLUSIVE end, exactly like Alpaca — that is what makes
    the harness's and production's ``b.ts < end`` filters meaningful. If this
    fake filtered the boundary bar itself, the lookahead test below would
    prove nothing.
    """

    def __init__(self) -> None:
        self.data: dict[tuple[str, str], list[Bar]] = {}
        self.calls: list[tuple[str, int]] = []

    def add(self, timeframe: str, bars: list[Bar]) -> None:
        for b in bars:
            self.data.setdefault((timeframe, b.symbol), []).append(b)
        for key in self.data:
            self.data[key].sort(key=lambda b: b.ts)

    def get_bars_multi(self, symbols, asset_class, timeframe, start, end):
        assert asset_class == "equity"
        self.calls.append((timeframe, len(symbols)))
        out = {}
        for s in symbols:
            bars = [b for b in self.data.get((timeframe, s), [])
                    if start <= b.ts <= end]
            if bars:
                out[s] = bars
        return out

    def get_bars(self, symbol, asset_class, timeframe, start, end,
                 use_cache=True):
        return self.get_bars_multi([symbol], asset_class, timeframe,
                                   start, end).get(symbol, [])


def daily_bars(source: FakeSource, symbol: str, prev_close: float,
               day: date = DAY, prev: date = PREV) -> None:
    """A prior-session daily bar plus a decoy bar ON the test day.

    ``OpeningDriveLoop.fetch_prev_closes`` must ignore the test day's own
    daily bar; the decoy is priced far away so a regression there is loud.
    """
    source.add("1Day", [
        mk_bar(symbol, ny_dt(prev, 0, 0), prev_close, prev_close,
               prev_close, prev_close, 1_000_000.0),
        mk_bar(symbol, ny_dt(day, 0, 0), 999.0, 999.0, 999.0, 999.0,
               1_000_000.0),
    ])


def opening_range(symbol: str, day: date = DAY, *,
                  final_close: float = 101.90,
                  final_high: float = 102.00,
                  vol: float = 1_000.0) -> list[Bar]:
    """The 30 bars covering 09:30-09:59 (bar timestamps are bar OPEN)."""
    bars = [
        mk_bar(symbol, ny_dt(day, 9, 30) + timedelta(minutes=i),
               100.50, 100.60, 100.40, 100.55, vol)
        for i in range(29)
    ]
    bars.append(mk_bar(
        symbol, ny_dt(day, 9, 30) + timedelta(minutes=29),
        100.55, final_high, 100.55, final_close, vol,
    ))
    return bars


def spy_session(source: FakeSource, day: date = DAY,
                or_close: float = 500.50) -> None:
    """SPY benchmark: +0.10% opening range. Without it the scanner refuses
    to cut at all (rs_atr would be unbenchmarked)."""
    daily_bars(source, "SPY", 500.0, day=day)
    bars = [
        mk_bar("SPY", ny_dt(day, 9, 30) + timedelta(minutes=i),
               500.0, 500.60, 499.90, 500.20, 50_000.0)
        for i in range(29)
    ]
    bars.append(mk_bar("SPY", ny_dt(day, 9, 30) + timedelta(minutes=29),
                       500.20, 500.60, 500.10, or_close, 50_000.0))
    source.add("1Min", bars)


def entry_window(symbol: str, day: date = DAY, *,
                 trigger: bool = True,
                 hostile_1100: bool = False) -> list[Bar]:
    """1-minute bars 10:00-10:59, with the trigger at 10:05.

    10:00-10:04 set the structural low to 101.00 (that becomes the stop);
    10:05 is the OR-high reclaim; the rest idle.
    """
    out: list[Bar] = []
    for i in range(5):
        out.append(mk_bar(symbol, ny_dt(day, 10, 0) + timedelta(minutes=i),
                          101.90, 101.95, 101.00, 101.50, 500.0))
    if trigger:
        out.append(mk_bar(symbol, ny_dt(day, 10, 5),
                          102.00, 102.30, 101.80, 102.20, 3_000.0))
    else:
        # Same shape, but volume below volume_confirm_mult * avg.
        out.append(mk_bar(symbol, ny_dt(day, 10, 5),
                          102.00, 102.30, 101.80, 102.20, 100.0))
    for i in range(6, 60):
        out.append(mk_bar(symbol, ny_dt(day, 10, 0) + timedelta(minutes=i),
                          102.20, 102.40, 102.00, 102.30, 500.0))
    if hostile_1100:
        # Timestamped exactly AT the entry-window end. The harness filters
        # `b.ts < end`, so this bar must never reach the position manager —
        # if it did, the position would stop out at 101.00 here.
        out.append(mk_bar(symbol, ny_dt(day, 11, 0),
                          102.30, 102.40, 50.00, 60.00, 9_999.0))
    return out


def managed_bars(symbol: str, day: date = DAY, *,
                 first: tuple[float, float, float, float] | None = None,
                 count: int = 54) -> list[Bar]:
    """5-minute bars from 11:00, benign unless ``first`` overrides bar 1.

    Benign means: never reaches the stop (101.00), never reaches the target
    (104.60), and never reaches the 1R breakeven trigger
    (102.220440 + 1.220440 = 103.440880) — so a bar-count assertion is not
    confounded by a breakeven stop move.
    """
    out: list[Bar] = []
    for i in range(count):
        ts = ny_dt(day, 11, 0) + timedelta(minutes=5 * i)
        if i == 0 and first is not None:
            o, h, l, c = first
        else:
            o, h, l, c = 102.30, 102.50, 102.10, 102.30
        out.append(mk_bar(symbol, ts, o, h, l, c, 5_000.0))
    return out


def baseline(atr: float = ATR, or_vol: float = AVG_OR_VOL,
             adv: float = 500_000.0) -> OpeningDriveBaseline:
    return OpeningDriveBaseline(
        atr_14d=atr, avg_or_volume_20d=or_vol, avg_daily_volume_20d=adv,
        computed_at=ny_dt(PREV, 16, 10),
    )


def load_cfg(**overrides: object) -> dict:
    with open(CONFIG_PATH) as f:
        cfg = yaml.safe_load(f)
    cfg = copy.deepcopy(cfg)
    apply_overrides(cfg, [f"{k}={v}" for k, v in overrides.items()])
    return cfg


def build(source: FakeSource, universe: dict[str, str], cfg: dict,
          *, screen_only: bool = False,
          baselines: dict | None = None) -> OpeningDriveBacktest:
    bl = baselines if baselines is not None else {
        sym: baseline() for sym in universe
    }
    return OpeningDriveBacktest(
        cfg, source, universe, EQUITY,
        baselines_for=lambda day: bl, screen_only=screen_only,
    )


def one_symbol_session(*, trigger: bool = True, hostile_1100: bool = False,
                       first_managed=None, managed_count: int = 54,
                       or_final_close: float = 101.90,
                       or_final_high: float = 102.00) -> FakeSource:
    src = FakeSource()
    spy_session(src)
    daily_bars(src, "AAA", PREV_CLOSE)
    src.add("1Min", opening_range("AAA", final_close=or_final_close,
                                  final_high=or_final_high))
    src.add("1Min", entry_window("AAA", trigger=trigger,
                                 hostile_1100=hostile_1100))
    src.add("5Min", managed_bars("AAA", first=first_managed,
                                 count=managed_count))
    return src


UNIVERSE_1 = {"AAA": "Information Technology"}


# ─────────────────────────────────────────────────────────────────────────
# The screen
# ─────────────────────────────────────────────────────────────────────────


def test_screen_selects_the_candidate_with_expected_metrics():
    src = one_symbol_session()
    bt = build(src, UNIVERSE_1, load_cfg(), screen_only=True)
    out = bt.run_session(DAY)

    assert out.kept == 1
    assert out.qualifiers == 1
    scan, rank = bt.recorder.scan_by_symbol["AAA"]
    m = scan.metrics
    assert m.or_high == pytest.approx(102.00)
    assert m.or_low == pytest.approx(100.40)
    assert m.or_close == pytest.approx(101.90)
    # 30 bars x 1_000. If the 10:00 bar had leaked in this would be 30_500.
    assert m.or_volume == pytest.approx(30_000.0)
    assert m.bar_coverage == pytest.approx(1.0)
    assert m.rvol_or == pytest.approx(3.0)
    assert m.disp_atr == pytest.approx(0.95)
    assert m.or_width_atr == pytest.approx(0.80)
    assert m.clv == pytest.approx(1.5 / 1.6)
    assert m.rs_atr == pytest.approx((0.019 - 0.001) / 0.02)
    assert scan.score == pytest.approx(3.0 * 0.9)
    assert rank == 1


def test_no_trigger_without_volume_confirmation_produces_no_trade():
    src = one_symbol_session(trigger=False)
    bt = build(src, UNIVERSE_1, load_cfg())
    out = bt.run_session(DAY)
    assert out.kept == 1
    assert out.triggers == 0
    assert out.trades == []


# ─────────────────────────────────────────────────────────────────────────
# LOOKAHEAD — the harness must not be able to see past 10:00
# ─────────────────────────────────────────────────────────────────────────


def test_post_1000_move_cannot_reach_the_screen():
    """A 10:00 bar that would flip the screen's verdict must be invisible.

    The opening range here closes weak: or_close 100.55 against or_high
    102.00 and or_low 100.40, so

        clv = (100.55 - 100.40) / (102.00 - 100.40) = 0.09375

    which fails ``min_clv: 0.6``. Every other gate is deliberately made to
    pass (prev_close 99.00 puts disp_atr at 0.775), so ``min_clv`` is the
    single gate standing between this symbol and the watchlist. The 10:00
    bar then closes at 102.00 on 500x volume. If that bar leaked into the
    opening range, or_close would become 102.00, clv would be ~1.0,
    or_volume would jump 18x, and the symbol would qualify. It must not.
    """
    prev = 99.00
    src = FakeSource()
    spy_session(src)
    daily_bars(src, "AAA", prev)
    # Strong high early, weak close at the cut -> fails min_clv.
    weak = [
        mk_bar("AAA", ny_dt(DAY, 9, 30), 100.50, 102.00, 100.40, 100.55,
               1_000.0),
    ] + [
        mk_bar("AAA", ny_dt(DAY, 9, 30) + timedelta(minutes=i),
               100.50, 100.60, 100.40, 100.55, 1_000.0)
        for i in range(1, 30)
    ]
    src.add("1Min", weak)
    # The future: a big up-bar timestamped exactly AT the cut.
    src.add("1Min", [
        mk_bar("AAA", ny_dt(DAY, 10, 0), 100.55, 102.10, 100.55, 102.00,
               500_000.0),
    ])
    src.add("1Min", entry_window("AAA")[1:])

    bt = build(src, UNIVERSE_1, load_cfg(), screen_only=True)
    out = bt.run_session(DAY)

    assert out.kept == 0, "post-10:00 information leaked into the screen"
    assert out.rejects.get("min_clv") == 1

    # Control: the identical bar placed INSIDE the opening range does
    # qualify, which proves the test is discriminating and not just failing
    # for some unrelated reason.
    src2 = FakeSource()
    spy_session(src2)
    daily_bars(src2, "AAA", prev)
    inside = weak[:-1] + [
        mk_bar("AAA", ny_dt(DAY, 9, 59), 100.55, 102.10, 100.55, 102.00,
               500_000.0),
    ]
    src2.add("1Min", inside)
    src2.add("1Min", entry_window("AAA"))
    bt2 = build(src2, UNIVERSE_1, load_cfg(), screen_only=True)
    out2 = bt2.run_session(DAY)
    assert out2.kept == 1


def test_entry_window_end_bar_is_not_managed():
    """A bar stamped 11:00 belongs to the managed phase, not the window.

    ``hostile_1100`` puts a low of 50.00 on the 11:00 one-minute bar. The
    trade must still exit on the managed 5-minute target bar; a stop exit
    here would mean the harness fed a bar at ``ts == end``.
    """
    src = one_symbol_session(
        hostile_1100=True, first_managed=(102.30, 104.70, 102.20, 104.50),
    )
    bt = build(src, UNIVERSE_1, load_cfg())
    out = bt.run_session(DAY)
    assert [t.exit_reason for t in out.trades] == ["target"]


# ─────────────────────────────────────────────────────────────────────────
# Hand-computed winner and loser
# ─────────────────────────────────────────────────────────────────────────


def test_winning_trade_R_by_arithmetic():
    """Target exit.

        entry fill   102.20 * 1.0002        = 102.220440
        initial risk 102.220440 - 101.00    =   1.220440
        qty          floor(7_000 / 102.20)  =  68
        exit         104.60 (resting limit, no slippage)
        R_gross      (104.60 - 102.220440) / 1.220440
                   =   2.379560 / 1.220440  =   1.9497476...
        pnl_gross    2.379560 * 68          = 161.81008
        commission   0.005 * 68 * 2         =   0.68
        pnl_net      161.81008 - 0.68       = 161.13008
        R_net        161.13008 / (1.220440 * 68) = 1.9415...
    """
    src = one_symbol_session(first_managed=(102.30, 104.70, 102.20, 104.50))
    cfg = load_cfg(**{"asset_classes.equity.commission_per_share": 0.005})
    bt = build(src, UNIVERSE_1, cfg)
    out = bt.run_session(DAY)

    assert len(out.trades) == 1
    t = out.trades[0]
    assert t.exit_reason == "target"
    assert t.entry_px == pytest.approx(ENTRY_FILL)
    assert t.stop_px == pytest.approx(STOP)
    assert t.target_px == pytest.approx(TARGET)
    assert t.qty == QTY
    assert t.exit_px == pytest.approx(TARGET)
    assert t.risk_per_share == pytest.approx(INITIAL_RISK)

    expected_gross_R = (TARGET - ENTRY_FILL) / INITIAL_RISK
    assert expected_gross_R == pytest.approx(2.379560 / 1.220440, rel=1e-9)
    assert t.R_gross == pytest.approx(expected_gross_R)
    assert t.pnl_gross == pytest.approx((TARGET - ENTRY_FILL) * QTY)
    assert t.pnl_gross == pytest.approx(161.81008, abs=1e-4)
    assert t.commission == pytest.approx(0.68)
    assert t.pnl_net == pytest.approx(161.13008, abs=1e-4)
    assert t.R_net == pytest.approx(161.13008 / (INITIAL_RISK * QTY),
                                    abs=1e-6)
    # Exit is the 11:00 managed bar, entry the 10:05 trigger bar.
    assert t.hold_minutes == pytest.approx(55.0)


def test_losing_trade_is_exactly_minus_one_R():
    """Stop exit. The stop sits 1.220440 below the fill, so R_gross is
    exactly -1.0 by construction:

        R_gross   (101.00 - 102.220440) / 1.220440 = -1.0
        pnl_gross -1.220440 * 68                   = -82.98992
        commission 0.005 * 68 * 2                  =   0.68
        pnl_net                                    = -83.66992
    """
    src = one_symbol_session(first_managed=(102.30, 102.40, 100.50, 100.80))
    cfg = load_cfg(**{"asset_classes.equity.commission_per_share": 0.005})
    bt = build(src, UNIVERSE_1, cfg)
    out = bt.run_session(DAY)

    assert len(out.trades) == 1
    t = out.trades[0]
    assert t.exit_reason == "stop"
    assert t.exit_px == pytest.approx(STOP)
    assert t.R_gross == pytest.approx(-1.0)
    assert t.pnl_gross == pytest.approx(-82.98992, abs=1e-4)
    assert t.pnl_net == pytest.approx(-83.66992, abs=1e-4)
    assert t.R_net == pytest.approx(-83.66992 / (INITIAL_RISK * QTY),
                                    abs=1e-6)


def test_stop_wins_when_one_bar_spans_stop_and_target():
    """The single most common way a backtest flatters itself.

    This bar's low (100.50) is through the stop and its high (104.70) is
    through the target. The exit must be the stop.
    """
    src = one_symbol_session(first_managed=(102.30, 104.70, 100.50, 103.00))
    bt = build(src, UNIVERSE_1, load_cfg())
    out = bt.run_session(DAY)

    assert len(out.trades) == 1
    t = out.trades[0]
    assert t.exit_reason == "stop"
    assert t.exit_px == pytest.approx(STOP)
    assert t.R_gross == pytest.approx(-1.0)


# ─────────────────────────────────────────────────────────────────────────
# Exits: time stop and the 15:30 flatten
# ─────────────────────────────────────────────────────────────────────────


def test_time_stop_fires_on_the_37th_distinct_five_minute_bar():
    """``max_hold_bars: 36`` counted from the first managed bar at 11:00.

    PositionManager exits when ``bars_held > max_hold_bars``, so the exit
    lands on bar 37: 11:00 + 36 * 5 min = 14:00.
    """
    src = one_symbol_session()
    bt = build(src, UNIVERSE_1, load_cfg())
    out = bt.run_session(DAY)

    assert len(out.trades) == 1
    t = out.trades[0]
    assert t.exit_reason == "time_stop"
    assert t.exit_ts == ny_dt(DAY, 14, 0).isoformat()
    # Market close: the bar's close, moved adversely by slippage.
    assert t.exit_px == pytest.approx(102.30 * (1 - SLIP))


def test_1530_flatten_closes_anything_still_open():
    """With the time stop pushed out of reach, the 15:30 flatten is the
    backstop and prices at the close of the bar ENDING at 15:30 (the bar
    stamped 15:25)."""
    src = one_symbol_session()
    cfg = load_cfg(**{"position_management.max_hold_bars": 1000})
    bt = build(src, UNIVERSE_1, cfg)
    out = bt.run_session(DAY)

    assert len(out.trades) == 1
    t = out.trades[0]
    assert t.exit_reason == "eod_flat"
    assert t.exit_px == pytest.approx(102.30 * (1 - SLIP))
    assert bt.book.count() == 0, "position left open past the flatten"
    # 15:25 is the last managed bar; nothing at or after 15:30 was fed.
    assert t.exit_ts == ny_dt(DAY, 15, 30).isoformat()
    # The flatten is not a PositionAction, so it never reaches DailyLedger.
    # The equity the harness sizes against must still include it, or every
    # EOD-flat trade's P&L would vanish from the compounding path.
    assert out.end_equity == pytest.approx(EQUITY + t.pnl_net)
    assert bt._equity_now() == pytest.approx(EQUITY + t.pnl_net)


def test_entry_window_stop_fills_because_the_oco_is_already_resting():
    """A stop touched between 10:00 and 11:00 must fill there, not at 11:00.

    Live, the OCO bracket is attached the moment the market entry fills, so
    the stop is a real resting order from 10:05 onward. The production loop
    only runs PositionManager from 11:00, so if the harness copied that
    literally it would silently discard every position that stopped out
    inside the entry window and recovered by 11:00 — a pure, invisible gift.

    Here the 10:06 bar trades down to 100.40, through the 101.00 stop, and
    the managed 5-minute bars are benign. The exit must be a stop stamped
    10:06.
    """
    src = FakeSource()
    spy_session(src)
    daily_bars(src, "AAA", PREV_CLOSE)
    src.add("1Min", opening_range("AAA"))
    bars = [b for b in entry_window("AAA") if b.ts != ny_dt(DAY, 10, 6)]
    bars.append(mk_bar("AAA", ny_dt(DAY, 10, 6),
                       102.20, 102.30, 100.40, 100.90, 500.0))
    src.add("1Min", bars)
    src.add("5Min", managed_bars("AAA"))

    bt = build(src, UNIVERSE_1, load_cfg())
    out = bt.run_session(DAY)

    assert len(out.trades) == 1
    t = out.trades[0]
    assert t.exit_reason == "stop"
    assert t.exit_ts == ny_dt(DAY, 10, 6).isoformat()
    assert t.R_gross == pytest.approx(-1.0)
    # And the time-stop clock is not confused by those entry-window bars:
    # nothing is left open to reach the managed phase.
    assert bt.book.count() == 0


def test_managed_phase_never_sees_a_bar_at_or_after_1530():
    src = one_symbol_session(managed_count=60)     # runs past 15:30
    cfg = load_cfg(**{"position_management.max_hold_bars": 1000})
    bt = build(src, UNIVERSE_1, cfg)
    out = bt.run_session(DAY)
    assert out.max_managed_bar_ts == ny_dt(DAY, 15, 25)


# ─────────────────────────────────────────────────────────────────────────
# Point-in-time baselines
# ─────────────────────────────────────────────────────────────────────────


def test_point_in_time_baselines_exclude_the_test_day():
    """A deliberately extreme test-day OR volume must not move that day's
    ``avg_or_volume_20d``.

    25 prior sessions each print 1_000 shares per opening-range minute
    (30_000 per session). The test day prints 3_000_000 in its opening range
    — 100x. The baseline handed to the scanner for the test day must be
    30_000 exactly.
    """
    src = FakeSource()
    sessions = [date(2026, 2, 2) + timedelta(days=i) for i in range(40)]
    sessions = [d for d in sessions if d.weekday() < 5][:26]
    test_day = sessions[-1]
    prior = sessions[:-1]

    for d in sessions:
        vol = 100_000.0 if d != test_day else 10_000_000.0
        src.add("1Day", [mk_bar("SPY", ny_dt(d, 0, 0), 500.0, 502.0, 498.0,
                                500.0, vol)])
        src.add("1Day", [mk_bar("AAA", ny_dt(d, 0, 0), 100.0, 102.0, 98.0,
                                100.0, vol)])
        per_min = 1_000.0 if d != test_day else 100_000.0
        for sym in ("SPY", "AAA"):
            src.add("1Min", [
                mk_bar(sym, ny_dt(d, 9, 30) + timedelta(minutes=i),
                       100.0, 100.5, 99.5, 100.0, per_min)
                for i in range(30)
            ])
            # The inclusive-end bar at the cut, with even more volume.
            src.add("1Min", [
                mk_bar(sym, ny_dt(d, 10, 0), 100.0, 100.5, 99.5, 100.0,
                       9_999_999.0),
            ])

    bl = point_in_time_baselines(
        src, ["AAA", "SPY"], prior[-1],
        or_minutes=30, lookback_sessions=20, cache_dir=None,
    )
    assert bl["AAA"].avg_or_volume_20d == pytest.approx(30_000.0)
    assert bl["AAA"].avg_daily_volume_20d == pytest.approx(100_000.0)
    # And the ATR is built from the prior sessions' daily bars only.
    assert bl["AAA"].atr_14d == pytest.approx(4.0)
    assert bl["AAA"].computed_at == ny_dt(prior[-1], 16, 10)


def test_committed_baselines_file_is_never_read(monkeypatch):
    """The harness must build baselines itself, not load the live snapshot.

    ``runtime/opening_drive/baselines.json`` holds ONE snapshot computed
    today. Applied to a session eight months ago it would leak eight months
    of future volume and volatility into the screen. The only route to that
    file is ``load_baselines``, so poison it: if any part of the session path
    reads baselines from disk, the session fails loudly.
    """
    import scripts.backtest_opening_drive as mod
    import strategies.opening_drive_scanner as scanner_mod

    def boom(path):
        raise AssertionError(f"harness read baselines from disk: {path}")

    monkeypatch.setattr(scanner_mod, "load_baselines", boom)
    monkeypatch.setattr(mod, "load_baselines", boom)

    src = one_symbol_session(first_managed=(102.30, 104.70, 102.20, 104.50))
    bt = build(src, UNIVERSE_1, load_cfg())
    out = bt.run_session(DAY)
    assert out.kept == 1
    assert [t.exit_reason for t in out.trades] == ["target"]


# ─────────────────────────────────────────────────────────────────────────
# Portfolio caps
# ─────────────────────────────────────────────────────────────────────────


def _multi_symbol_session(symbols: dict[str, str]) -> FakeSource:
    src = FakeSource()
    spy_session(src)
    for sym in symbols:
        daily_bars(src, sym, PREV_CLOSE)
        src.add("1Min", opening_range(sym))
        src.add("1Min", entry_window(sym))
        src.add("5Min", managed_bars(sym))
    return src


def test_max_concurrent_positions_binds():
    """Six identical candidates across three sectors; only five may enter.

    ``max_concurrent_positions: 5``, and the sector cap is not the binding
    constraint here because no sector holds more than two.
    """
    universe = {
        "AAA": "Information Technology", "BBB": "Information Technology",
        "CCC": "Health Care", "DDD": "Health Care",
        "EEE": "Energy", "FFF": "Energy",
    }
    src = _multi_symbol_session(universe)
    bt = build(src, universe, load_cfg())
    out = bt.run_session(DAY)

    assert out.kept == 6
    assert out.triggers == 6
    assert out.entries == 5
    assert len(out.trades) == 5
    reasons = bt.capture.risk_rejects
    assert reasons.get("concurrent_position") == 1


def test_max_per_sector_binds():
    """Three candidates in one sector; ``max_per_sector: 2`` allows two."""
    universe = {
        "AAA": "Information Technology", "BBB": "Information Technology",
        "CCC": "Information Technology",
    }
    src = _multi_symbol_session(universe)
    bt = build(src, universe, load_cfg())
    out = bt.run_session(DAY)

    assert out.kept == 3
    assert out.triggers == 3
    assert out.entries == 2
    assert bt.capture.risk_rejects.get("sector_exposure") == 1


def test_slots_are_first_come_first_served_by_trigger_time():
    """Spec 7.1: a later-ranked name that triggers first takes the slot."""
    universe = {"AAA": "Energy", "BBB": "Energy", "CCC": "Energy"}
    src = FakeSource()
    spy_session(src)
    for sym in universe:
        daily_bars(src, sym, PREV_CLOSE)
        src.add("1Min", opening_range(sym))
        src.add("5Min", managed_bars(sym))
    # AAA and BBB trigger at 10:05; CCC — ranked last, alphabetically and by
    # nothing else, since all three score identically — triggers at 10:04,
    # one minute ahead of both. 10:04 is the last bar whose low reaches
    # 101.00, so CCC's stop is not revisited later in the window.
    src.add("1Min", entry_window("AAA"))
    src.add("1Min", entry_window("BBB"))
    ccc = [b for b in entry_window("CCC")
           if b.ts not in (ny_dt(DAY, 10, 4), ny_dt(DAY, 10, 5))]
    ccc.append(mk_bar("CCC", ny_dt(DAY, 10, 4),
                      102.00, 102.30, 101.80, 102.20, 3_000.0))
    ccc.append(mk_bar("CCC", ny_dt(DAY, 10, 5),
                      102.20, 102.40, 102.00, 102.30, 500.0))
    src.add("1Min", ccc)

    bt = build(src, universe, load_cfg())
    out = bt.run_session(DAY)
    entered = sorted({t.symbol for t in out.trades})
    assert "CCC" in entered, "the earliest trigger must get a slot"
    assert len(entered) == 2, "max_per_sector: 2 must cap the sector"
    assert bt.capture.risk_rejects.get("sector_exposure") == 1


# ─────────────────────────────────────────────────────────────────────────
# Aggregation / reporting
# ─────────────────────────────────────────────────────────────────────────


def test_summary_rolls_up_pnl_and_drawdown():
    src = one_symbol_session(first_managed=(102.30, 102.40, 100.50, 100.80))
    bt = build(src, UNIVERSE_1, load_cfg())
    out = bt.run([DAY])
    s = summarize(out)
    assert s["trades"] == 1
    assert s["win_rate"] == 0.0
    assert s["sessions"] == 1
    assert s["candidates_per_day"] == pytest.approx(1.0)
    assert s["exit_reasons"]["stop"] == 1
    assert s["total_return_pct"] < 0
    assert s["net_pnl"] == pytest.approx(-INITIAL_RISK * QTY, abs=1e-4)


def test_screen_only_takes_no_trades():
    src = one_symbol_session(first_managed=(102.30, 104.70, 102.20, 104.50))
    bt = build(src, UNIVERSE_1, load_cfg(), screen_only=True)
    out = bt.run_session(DAY)
    assert out.kept == 1
    assert out.trades == []
    assert bt.book.count() == 0


def test_apply_overrides_rejects_unknown_keys():
    cfg = load_cfg()
    with pytest.raises(SystemExit):
        apply_overrides(cfg, ["scanner.filters.nope=1"])
    with pytest.raises(SystemExit):
        apply_overrides(cfg, ["no.such.section=1"])
    apply_overrides(cfg, ["scanner.filters.min_avg_daily_volume=50000"])
    assert cfg["scanner"]["filters"]["min_avg_daily_volume"] == 50000


# ─────────────────────────────────────────────────────────────────────────
# Cache
# ─────────────────────────────────────────────────────────────────────────


class _StubClient:
    def __init__(self) -> None:
        self.calls = 0

    def get_stock_bars_multi(self, symbols, timeframe, start, end,
                             limit=10000, chunk_size=200):
        self.calls += 1
        return {
            s: [{"t": (start + timedelta(minutes=i)).isoformat().replace(
                    "+00:00", "Z"),
                 "o": 10.0, "h": 10.5, "l": 9.5, "c": 10.2, "v": 100}
                for i in range(3)]
            for s in symbols
        }


def test_cache_serves_repeat_and_subset_requests_without_refetching(tmp_path):
    client = _StubClient()
    src = CachedBarSource(client, tmp_path)
    start = ny_dt(DAY, 9, 30)
    end = ny_dt(DAY, 10, 0)

    first = src.get_bars_multi(["AAA", "BBB"], "equity", "1Min", start, end)
    assert set(first) == {"AAA", "BBB"}
    assert client.calls == 1

    # Same window again, in a fresh process (new object, same dir).
    src2 = CachedBarSource(client, tmp_path)
    again = src2.get_bars_multi(["AAA", "BBB"], "equity", "1Min", start, end)
    assert client.calls == 1
    assert [b.close for b in again["AAA"]] == [b.close for b in first["AAA"]]

    # A sweep asking for a SUBSET must also hit cache.
    subset = src2.get_bars_multi(["AAA"], "equity", "1Min", start, end)
    assert client.calls == 1
    assert len(subset["AAA"]) == 3

    # A new symbol fetches only the missing one.
    src2.get_bars_multi(["AAA", "CCC"], "equity", "1Min", start, end)
    assert client.calls == 2


def test_cache_can_refuse_to_fetch(tmp_path):
    from scripts.backtest_opening_drive import CacheMiss
    src = CachedBarSource(_StubClient(), tmp_path, allow_fetch=False)
    with pytest.raises(CacheMiss):
        src.get_bars_multi(["AAA"], "equity", "1Min",
                           ny_dt(DAY, 9, 30), ny_dt(DAY, 10, 0))
