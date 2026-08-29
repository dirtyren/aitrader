"""Historical backtest harness for the Opening Drive intraday equity strategy.

The whole point of this file is that it does NOT contain any screening,
trigger, sizing or exit logic. Every decision is delegated to the exact
production objects the live trader uses:

    strategies/opening_drive_scanner.py  OpeningDriveScanner.run_cut,
                                         compute_or_metrics, gate_reason,
                                         rank_score
    strategies/setup_opening_drive.py    OpeningDriveSetup.check
    scheduler/opening_drive_loop.py      OpeningDriveLoop.run_cut / on_bar /
                                         switch_to_regular_session_bars /
                                         manage_open / force_close_all
    risk/manager.py + risk/sizing.py     RiskManager.evaluate, size_position
    risk/filters.py                      the pipeline built by
                                         main_opening_drive.build_pipeline
                                         (ConcurrentPositionFilter,
                                         SectorExposureFilter, ...)
    core/position_manager.py             stop / target / breakeven /
                                         max_hold_bars
    broker/order_executor.py             the real OrderExecutor
    scripts/build_opening_drive_baselines.py  build_baselines

Only three things are simulated, and they are all confined to this file:

  1. ``CachedBarSource`` — a disk-cached stand-in for ``AlpacaData`` that
     answers ``get_bars_multi``. Bars are real IEX bars from Alpaca.
  2. ``BacktestBroker`` — a stand-in for ``AlpacaClient``. It owns the FILL
     MODEL and nothing else.
  3. ``RecordingExecutor`` — a transparent shim around the real
     ``OrderExecutor`` that records trade rows. It makes no decisions.

SIDE. ``--side short`` inverts the ACTION, not the trigger: the screen, the
gates and the trigger test are untouched, ``OpeningDriveSetup`` mirrors stop
and target around the entry, and ``PositionManager`` already manages both
sides. Default ``long``, so existing behaviour is byte-identical.

FILL MODEL. Two modes. The optimistic one is the default and is what every
earlier run used; ``--realistic-costs`` is the honest one. Every assumption
is stated in the report with the direction it biases the result.

  OPTIMISTIC (default):

  * Entry: the trigger bar's CLOSE, moved adversely by ``slippage_bps``.
    Live the entry is a market order sent after the bar closes, so the real
    fill is the next print — optimistic by roughly a spread.
  * Stop: fills exactly at the stop price. Optimistic: real stops gap
    through, especially on the intraday momentum names this screen selects.
  * Target: fills exactly at the target price (it is a resting limit, so
    this one is fair).
  * time_stop / 15:30 flatten: the bar's close, moved adversely by
    ``slippage_bps`` (market orders).

  --realistic-costs, additionally:

  * Entry: adverse by ``slippage_bps`` PLUS 0.05 x the trigger bar's range.
    A market order sent on the close of a bar that just travelled that range
    does not get the close.
  * Stop: adverse by 0.10 x the average 1-minute TRUE RANGE over that
    symbol's own opening range — a per-bar figure, deliberately not
    ``atr_14d / 390``, which would understate an opening-range gap by an
    order of magnitude. And if the bar OPENED already beyond the stop, the
    fill is the open when that is worse. This is the gap-through the
    optimistic model ignores entirely.
  * Target: still exactly at the target, still only when the bar actually
    traded through it (``PositionManager`` checks ``high >= target`` /
    ``low <= target`` before emitting). A resting limit, so this is fair.
  * Short borrow: modelled as ZERO. Every position is intraday-only, so
    there is no overnight borrow — but a hard-to-borrow name would break
    that assumption and this harness cannot see borrow rates.

  BOTH MODES:

  * Stop-before-target when one bar spans both. Not a harness choice:
    ``PositionManager._check_position`` tests the stop first, and that is
    the production code path. The favourable side is never taken.
  * ``commission_per_share`` charged on both legs.

BOUNDARY RULE. Alpaca's ``end`` is inclusive, so every window request
returns one extra bar. Every consumer here filters ``b.ts < end``. For the
opening range that is load-bearing: the 10:00 bar is the first bar of the
entry window and folding it into or_high / or_close is a real lookahead.
``OpeningDriveLoop.run_cut`` does that filtering in production and this
harness drives that method rather than reimplementing it.

Usage:
    python scripts/backtest_opening_drive.py \
        --config config/settings_opening_drive_equity.yaml \
        --start 2025-08-29 --end 2026-08-28 --equity 100000

    # screening statistics only, no entries or management
    ... --screen-only

    # parameter override for sweeps (repeatable)
    ... --set scanner.filters.min_avg_daily_volume=50000

    # the inverted hypothesis, honest costs, on the hold-out
    ... --start 2020-07-27 --end 2023-08-31 \
        --side short --realistic-costs --equity 100000
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import statistics
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, time as dt_time, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable, Sequence

import pandas as pd
import pytz
import yaml

# Same bootstrap as scripts/sweep_equity_strategy.py, so the harness runs
# both as `python scripts/backtest_opening_drive.py` and as a module.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from broker.alpaca_data import _bars_from_raw  # noqa: E402
from core.asset_class import AssetClassConfig  # noqa: E402
from core.bar import Bar  # noqa: E402
from core.position_manager import PositionManager  # noqa: E402
from risk.manager import RiskManager  # noqa: E402
from risk.sizing import SizingConfig  # noqa: E402
from scheduler.loop import record_exits_to_ledger  # noqa: E402
from scheduler.opening_drive_loop import OpeningDriveConfig, OpeningDriveLoop  # noqa: E402
from state.daily_ledger import DailyLedger  # noqa: E402
from state.position_book import PositionBook  # noqa: E402
from strategies.opening_drive_scanner import (
    OpeningDriveBaseline, OpeningDriveFilters, OpeningDriveScanner, ScanResult,
    load_baselines, load_universe, save_baselines,
)

logger = logging.getLogger("backtest_opening_drive")

_NY = pytz.timezone("America/New_York")

DEFAULT_CACHE_DIR = "runtime/backtest_cache"
DEFAULT_OUT_DIR = "runtime/backtest_out"

EXIT_REASONS = ("target", "stop", "time_stop", "eod_flat")


def ny_dt(day: date, hh: int, mm: int) -> datetime:
    """UTC datetime for a local NY wall-clock time on ``day``."""
    return _NY.localize(datetime.combine(day, dt_time(hh, mm))).astimezone(
        timezone.utc,
    )


def ny_date(ts: datetime) -> date:
    return ts.astimezone(_NY).date()


def avg_minute_true_range(bars: Sequence[Bar]) -> float:
    """Mean 1-minute TRUE range over ``bars``.

    True range, not high-low: a 1-minute bar that gapped from the previous
    close is exactly the case a stop-slippage estimate has to capture, and
    high-low alone would miss it.

    This is the per-bar volatility figure the realistic stop model needs.
    ``atr_14d / 390`` is NOT a substitute — it spreads a whole day's range
    evenly across 390 minutes, whereas the opening range is by far the most
    volatile stretch of the session, so it would understate stop slippage on
    precisely the bars where stops actually fire.
    """
    if not bars:
        return 0.0
    trs: list[float] = []
    prev_close: float | None = None
    for b in bars:
        if prev_close is None:
            trs.append(b.high - b.low)
        else:
            trs.append(max(b.high - b.low,
                           abs(b.high - prev_close),
                           abs(b.low - prev_close)))
        prev_close = b.close
    return sum(trs) / len(trs)


# ─────────────────────────────────────────────────────────────────────────
# Bar source: disk-cached, so re-runs and sweeps never re-hit the API
# ─────────────────────────────────────────────────────────────────────────


class CacheMiss(RuntimeError):
    """Raised when a window is not cached and fetching is disabled."""


class CachedBarSource:
    """``get_bars_multi``-compatible bar source with an on-disk cache.

    Cache layout, under ``cache_dir``::

        <timeframe>/<sha1(timeframe|start|end)>/bars.parquet
        <timeframe>/<sha1(timeframe|start|end)>/manifest.json

    The manifest records which symbols have been *requested* for that window,
    so a symbol that legitimately has no IEX prints is remembered as empty
    instead of being refetched forever. A request for a subset of a cached
    window is served from disk; a request that adds symbols fetches only the
    missing ones and merges them in. That is what makes a parameter sweep
    (which re-screens with a different watchlist) hit cache rather than the
    API.

    ``prime_daily`` fetches the whole daily range once and serves every later
    1Day slice from memory. Without it, ``fetch_prev_closes`` and
    ``build_baselines`` would each issue one near-identical 60-day universe
    request per session — hundreds of redundant calls.
    """

    def __init__(self, client, cache_dir: str | Path = DEFAULT_CACHE_DIR,
                 allow_fetch: bool = True) -> None:
        self.client = client
        self.root = Path(cache_dir)
        self.root.mkdir(parents=True, exist_ok=True)
        self.allow_fetch = allow_fetch
        self.api_fetches = 0
        self.cache_reads = 0
        self._mem: dict[str, dict[str, list[Bar]]] = {}
        self._daily: dict[str, list[Bar]] | None = None
        self._daily_range: tuple[datetime, datetime] | None = None

    # ── public API (duck-types AlpacaData) ──────────────────────────────

    def prime_daily(self, symbols: Sequence[str], start: datetime,
                    end: datetime) -> None:
        self._daily = self._window("1Day", start, end, list(symbols))
        self._daily_range = (start, end)

    def get_bars_multi(self, symbols: list[str], asset_class: str,
                       timeframe: str, start: datetime,
                       end: datetime) -> dict[str, list[Bar]]:
        if asset_class != "equity":
            raise ValueError(f"equity only, got {asset_class!r}")
        if (timeframe == "1Day" and self._daily is not None
                and self._daily_range is not None
                and self._daily_range[0] <= start
                and end <= self._daily_range[1]):
            # Alpaca's end is inclusive; mirror that so production code sees
            # identical semantics and its own `< end` filters stay meaningful.
            return {
                sym: [b for b in self._daily.get(sym, [])
                      if start <= b.ts <= end]
                for sym in symbols
                if self._daily.get(sym)
            }
        return self._window(timeframe, start, end, list(symbols))

    def get_bars(self, symbol: str, asset_class: str, timeframe: str,
                 start: datetime, end: datetime,
                 use_cache: bool = True) -> list[Bar]:
        got = self.get_bars_multi([symbol], asset_class, timeframe, start, end)
        return got.get(symbol, [])

    # ── internals ───────────────────────────────────────────────────────

    def _window_dir(self, timeframe: str, start: datetime,
                    end: datetime) -> Path:
        key = hashlib.sha1(
            f"{timeframe}|{start.isoformat()}|{end.isoformat()}".encode(),
        ).hexdigest()[:20]
        return self.root / timeframe / key

    def _window(self, timeframe: str, start: datetime, end: datetime,
                symbols: list[str]) -> dict[str, list[Bar]]:
        d = self._window_dir(timeframe, start, end)
        mem_key = str(d)
        state = self._mem.get(mem_key)
        if state is None:
            state = self._load_window(d)
            self._mem[mem_key] = state
        known: set[str] = set(state["_requested"])          # type: ignore[arg-type]
        missing = [s for s in symbols if s not in known]
        if missing:
            if not self.allow_fetch:
                raise CacheMiss(
                    f"{timeframe} {start.isoformat()}..{end.isoformat()} "
                    f"missing {len(missing)} symbols and fetching is disabled",
                )
            raw = self.client.get_stock_bars_multi(
                missing, timeframe, start, end,
            )
            self.api_fetches += 1
            for sym, rows in raw.items():
                bars = _bars_from_raw(rows, sym)
                if bars:
                    state[sym] = bars
            known.update(missing)
            state["_requested"] = sorted(known)             # type: ignore[assignment]
            self._save_window(d, state)
        else:
            self.cache_reads += 1
        return {s: state[s] for s in symbols if state.get(s)}

    def _load_window(self, d: Path) -> dict:
        manifest = d / "manifest.json"
        if not manifest.exists():
            return {"_requested": []}
        try:
            requested = json.loads(manifest.read_text())["symbols"]
        except (OSError, ValueError, KeyError):
            return {"_requested": []}
        state: dict = {"_requested": list(requested)}
        parquet = d / "bars.parquet"
        if parquet.exists():
            try:
                df = pd.read_parquet(parquet)
            except Exception as exc:                     # pragma: no cover
                logger.warning("cache read failed %s: %s", parquet, exc)
                return {"_requested": []}
            for sym, grp in df.groupby("symbol", sort=False):
                state[str(sym)] = [
                    Bar(symbol=str(sym), ts=r.ts.to_pydatetime(),
                        open=float(r.open), high=float(r.high),
                        low=float(r.low), close=float(r.close),
                        volume=float(r.volume))
                    for r in grp.itertuples(index=False)
                ]
        return state

    def _save_window(self, d: Path, state: dict) -> None:
        d.mkdir(parents=True, exist_ok=True)
        rows = [
            {"symbol": sym, "ts": b.ts, "open": b.open, "high": b.high,
             "low": b.low, "close": b.close, "volume": b.volume}
            for sym, bars in state.items()
            if sym != "_requested"
            for b in bars
        ]
        if rows:
            df = pd.DataFrame(rows)
            tmp = d / "bars.parquet.tmp"
            df.to_parquet(tmp, index=False)
            tmp.replace(d / "bars.parquet")
        tmp_m = d / "manifest.json.tmp"
        tmp_m.write_text(json.dumps({"symbols": state["_requested"]}))
        tmp_m.replace(d / "manifest.json")


# ─────────────────────────────────────────────────────────────────────────
# Broker stand-in: this object IS the fill model
# ─────────────────────────────────────────────────────────────────────────


class BacktestBroker:
    """Minimal AlpacaClient stand-in. Market orders fill at the current mark.

    ``set_mark`` is called by the driver with the bar close that the order is
    being sent against, so ``submit_order`` can return a ``filled_avg_price``
    the real ``OrderExecutor`` then stamps onto the ``OpenPosition``. Buys
    fill above the mark and sells below it by ``slippage_bps`` — never the
    favourable direction.

    ``extra_adverse`` is an absolute per-share penalty applied on top, in the
    same adverse direction. The driver sets it only on the trigger bar in the
    entry window (0.05 x bar range under ``--realistic-costs``); every other
    ``set_mark`` call resets it to zero, which is why it cannot leak into an
    exit. Exit prices are in any case recorded by ``TradeRecorder``, not read
    back off this object.
    """

    def __init__(self, slippage_bps: float) -> None:
        self.slip = float(slippage_bps) / 10_000.0
        self.marks: dict[str, float] = {}
        self.extra_adverse: dict[str, float] = {}
        self._seq = 0
        self.orders: list[dict] = []

    def set_mark(self, symbol: str, price: float,
                 extra_adverse: float = 0.0) -> None:
        self.marks[symbol] = float(price)
        self.extra_adverse[symbol] = float(extra_adverse)

    def _next(self, prefix: str) -> str:
        self._seq += 1
        return f"{prefix}-{self._seq}"

    def submit_order(self, symbol: str, qty: float, side: str,
                     order_type: str = "market", time_in_force: str = "day",
                     client_order_id: str | None = None,
                     limit_price: float | None = None,
                     extended_hours: bool = False, **_: object) -> dict:
        mark = self.marks.get(symbol)
        if mark is None:
            raise RuntimeError(f"no mark set for {symbol}")
        extra = self.extra_adverse.get(symbol, 0.0)
        # Adverse means worse for the position being taken: higher when
        # buying, lower when selling. Never the favourable direction.
        if side == "buy":
            fill = mark * (1 + self.slip) + extra
        else:
            fill = max(mark * (1 - self.slip) - extra, 0.0)
        order = {
            "id": self._next("ord"), "status": "filled",
            "filled_avg_price": fill, "symbol": symbol, "qty": qty,
            "side": side, "client_order_id": client_order_id,
        }
        self.orders.append(order)
        return order

    def attach_oco(self, symbol: str, qty: float, side: str,
                   stop_price: float, target_price: float,
                   time_in_force: str = "day",
                   client_order_id: str | None = None, **_: object) -> dict:
        oid = self._next("oco")
        return {
            "id": oid, "status": "new",
            "legs": [
                {"id": f"{oid}-stop", "type": "stop", "stop_price": stop_price},
                {"id": f"{oid}-tp", "type": "limit", "limit_price": target_price},
            ],
        }

    def list_orders(self, status: str | None = None,
                    symbols: list[str] | None = None,
                    nested: bool = False) -> list[dict]:
        # The simulated broker has no resting orders: exits are resolved by
        # PositionManager against the bar, so there is nothing to cancel.
        return []

    def cancel_order(self, order_id: str) -> None:
        return None

    def get_order(self, order_id: str) -> dict:
        return {"id": order_id, "status": "filled"}

    def replace_order(self, order_id: str, **_: object) -> dict:
        return {"id": order_id, "status": "new"}

    def get_positions(self) -> list[dict]:
        return []

    def get_account(self) -> dict:
        return {}


# ─────────────────────────────────────────────────────────────────────────
# Trade recording
# ─────────────────────────────────────────────────────────────────────────


@dataclass
class TradeRow:
    session: str
    symbol: str
    side: str
    sector: str
    rank: int
    score: float
    rvol_or: float
    disp_atr: float
    or_width_atr: float
    clv: float
    rs_atr: float
    or_high: float
    atr_14d: float
    entry_ts: str
    entry_px: float
    stop_px: float
    target_px: float
    risk_per_share: float
    qty: float
    notional: float
    exit_ts: str
    exit_px: float
    exit_reason: str
    breakeven_moved: bool
    hold_minutes: float
    R_gross: float
    R_net: float
    pnl_gross: float
    commission: float
    pnl_net: float


@dataclass
class _Live:
    pos_symbol: str
    setup: str
    side: str
    entry_px: float
    qty: float
    initial_stop: float
    target: float
    opened_at: datetime
    scan: ScanResult
    rank: int
    breakeven_moved: bool = False


class TradeRecorder:
    """Records trade rows. Makes no decisions and never mutates the book."""

    def __init__(self, slippage_bps: float,
                 commission_per_share: float,
                 realistic_costs: bool = False) -> None:
        self.slip = float(slippage_bps) / 10_000.0
        self.commission_per_share = float(commission_per_share)
        self.realistic_costs = bool(realistic_costs)
        self.rows: list[TradeRow] = []
        self.live: dict[tuple[str, str], _Live] = {}
        self.current_ts: datetime | None = None
        self.session: date | None = None
        self.scan_by_symbol: dict[str, tuple[ScanResult, int]] = {}
        self.marks: dict[str, float] = {}
        # Absolute per-share stop-slippage budget per symbol for this session:
        # 0.10 x the mean 1-minute true range of that symbol's own opening
        # range. Populated by the driver only under --realistic-costs.
        self.stop_slip: dict[str, float] = {}
        # The bar currently being processed. Needed for the realistic stop
        # rule's "if the bar OPENED beyond the stop, fill at the open".
        self.current_bar: Bar | None = None
        self.unpriced_flattens = 0

    def on_open(self, pos) -> None:
        scan, rank = self.scan_by_symbol.get(pos.symbol, (None, -1))
        if scan is None:
            return
        self.live[(pos.symbol, pos.setup)] = _Live(
            pos_symbol=pos.symbol, setup=pos.setup, side=pos.side,
            entry_px=pos.entry_px,
            qty=pos.qty, initial_stop=float(pos.initial_stop_px),
            target=float(pos.target_px), opened_at=pos.opened_at,
            scan=scan, rank=rank,
        )

    def on_breakeven(self, symbol: str, setup: str) -> None:
        live = self.live.get((symbol, setup))
        if live is not None:
            live.breakeven_moved = True

    def exit_price_for(self, kind: str, action_price: float,
                       side: str = "long", symbol: str | None = None,
                       bar: Bar | None = None) -> float:
        """Apply the fill model to an exit. Direction-mirrored by ``side``.

        target
            Fills exactly at its level in both modes. It is a resting limit,
            and ``PositionManager`` only emits it once the bar has actually
            traded through the level, so this is fair rather than optimistic.
        time_stop / eod_flat
            Market orders: the bar close moved adversely by
            ``slippage_bps`` — down when closing a long, UP when closing a
            short (closing a short is a buy).
        stop
            Optimistic mode fills exactly at the stop level, which real
            stops do not do. Realistic mode fills adverse by
            ``stop_slip[symbol]`` and, if the bar OPENED already beyond the
            stop, takes the open when that is worse of the two.
        """
        if kind in ("time_stop", "eod_flat"):
            return (action_price * (1 - self.slip) if side == "long"
                    else action_price * (1 + self.slip))
        if kind == "stop" and self.realistic_costs:
            slip = self.stop_slip.get(symbol or "", 0.0)
            if side == "long":
                px = action_price - slip
                if bar is not None:
                    px = min(px, bar.open)
                return px
            px = action_price + slip
            if bar is not None:
                px = max(px, bar.open)
            return px
        return action_price

    def on_exit(self, symbol: str, setup: str, kind: str,
                action_price: float, ts: datetime | None = None) -> None:
        live = self.live.pop((symbol, setup), None)
        if live is None:
            return
        exit_ts = ts or self.current_ts or live.opened_at
        bar = self.current_bar
        if bar is not None and bar.symbol != symbol:
            bar = None
        exit_px = self.exit_price_for(kind, action_price, side=live.side,
                                      symbol=symbol, bar=bar)
        # abs(): a short's initial stop sits ABOVE its entry. The magnitude is
        # the same structural distance either way, which is what makes R
        # comparable across the two sides.
        risk = abs(live.entry_px - live.initial_stop)
        sign = 1.0 if live.side == "long" else -1.0
        gross = sign * (exit_px - live.entry_px) * live.qty
        commission = self.commission_per_share * live.qty * 2.0
        net = gross - commission
        m = live.scan.metrics
        self.rows.append(TradeRow(
            session=str(self.session), symbol=symbol, side=live.side,
            sector=live.scan.sector,
            rank=live.rank, score=live.scan.score,
            rvol_or=m.rvol_or, disp_atr=m.disp_atr,
            or_width_atr=m.or_width_atr, clv=m.clv, rs_atr=m.rs_atr,
            or_high=m.or_high, atr_14d=m.atr_14d,
            entry_ts=live.opened_at.isoformat(), entry_px=live.entry_px,
            stop_px=live.initial_stop, target_px=live.target,
            risk_per_share=risk, qty=live.qty,
            notional=live.entry_px * live.qty,
            exit_ts=exit_ts.isoformat(), exit_px=exit_px, exit_reason=kind,
            breakeven_moved=live.breakeven_moved,
            hold_minutes=(exit_ts - live.opened_at).total_seconds() / 60.0,
            R_gross=(sign * (exit_px - live.entry_px) / risk) if risk > 0 else 0.0,
            R_net=(net / (risk * live.qty)) if risk > 0 and live.qty > 0 else 0.0,
            pnl_gross=gross, commission=commission, pnl_net=net,
        ))


class RecordingExecutor:
    """Transparent shim around the real ``OrderExecutor``.

    Every call is delegated; the shim only observes. It exists because
    production deliberately does not record exits itself (the reconciler is
    the writer of record for closes against real broker fills), so a
    backtest has to observe the ``PositionAction`` stream to build a ledger.
    """

    _EXITS = ("stop", "target", "time_stop")

    def __init__(self, inner, recorder: TradeRecorder,
                 broker: BacktestBroker) -> None:
        self._inner = inner
        self._rec = recorder
        self._broker = broker

    def __getattr__(self, name: str):
        return getattr(self._inner, name)

    def submit(self, signal, decision, asset_class: str):
        pos = self._inner.submit(signal, decision, asset_class=asset_class)
        if pos is not None:
            self._rec.on_open(pos)
        return pos

    def handle_actions(self, actions, asset_class: str,
                       parent_order_id: str | None = None) -> None:
        for a in actions:
            if a.kind == "breakeven":
                self._rec.on_breakeven(a.symbol, a.setup)
            elif a.kind in self._EXITS:
                self._broker.set_mark(a.symbol, a.price)
                self._rec.on_exit(a.symbol, a.setup, a.kind, a.price)
        self._inner.handle_actions(
            actions, asset_class=asset_class, parent_order_id=parent_order_id,
        )

    def close_position(self, symbol: str, side: str, qty: float, *,
                       setup: str, asset_class: str = "crypto"):
        # Only reached from OpeningDriveLoop.force_close_all — handle_actions
        # calls the inner executor's own close_position, not this one.
        mark = self._rec.marks.get(symbol)
        if mark is not None:
            self._broker.set_mark(symbol, mark)
            self._rec.on_exit(symbol, setup, "eod_flat", mark)
        return self._inner.close_position(
            symbol, side, qty, setup=setup, asset_class=asset_class,
        )


# ─────────────────────────────────────────────────────────────────────────
# Scanner log capture — the reject histogram without duplicating gate logic
# ─────────────────────────────────────────────────────────────────────────


class CutLogCapture(logging.Handler):
    """Reads the reject histograms out of production's own log records.

    ``run_cut`` builds the per-gate histogram internally but returns only the
    watchlist. Re-deriving it here would mean a second copy of the gate
    order — the exact duplication this harness exists to avoid — so the
    numbers are lifted from the record the production code already emits.
    The same trick collects the post-trigger risk rejections
    (``OD_REJECTED``) and the setup's wide-stop rejections.

    The taps only see records the source logger actually emits, so
    ``install`` raises those three loggers to INFO.
    """

    TAPPED = (
        "strategies.opening_drive_scanner",
        "scheduler.opening_drive_loop",
        "strategies.setup_opening_drive",
    )

    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self.risk_rejects: dict[str, int] = {}
        self.wide_stop_rejects = 0
        self.reset()

    def install(self) -> None:
        """Attach to the tapped loggers, replacing any earlier tap.

        Only the logger LEVEL is raised and a handler added — ``propagate``
        is deliberately left alone. Turning it off here silently broke two
        unrelated ``caplog`` tests in tests/test_opening_drive_loop.py, and
        muting production log records is not this harness's business.
        Console volume is controlled in ``main`` by setting the level on the
        root handler instead.
        """
        for name in self.TAPPED:
            lg = logging.getLogger(name)
            for h in list(lg.handlers):
                if isinstance(h, CutLogCapture):
                    lg.removeHandler(h)
            lg.setLevel(logging.INFO)
            lg.addHandler(self)

    def reset(self) -> None:
        self.qualifiers = 0
        self.kept = 0
        self.rejects: dict[str, int] = {}
        self.no_cut_reason: str | None = None

    def emit(self, record: logging.LogRecord) -> None:
        msg = str(record.msg)
        args = record.args if isinstance(record.args, tuple) else ()
        if msg.startswith("OD_CUT_DONE"):
            ints = [a for a in args if isinstance(a, int)]
            if len(ints) >= 2:
                self.qualifiers, self.kept = ints[0], ints[1]
            for a in args:
                if isinstance(a, dict):
                    self.rejects = {str(k): int(v) for k, v in a.items()}
        elif msg.startswith("OD_BENCHMARK_UNAVAILABLE"):
            self.no_cut_reason = "benchmark_unavailable"
        elif msg.startswith("OD_BASELINES_TOO_STALE"):
            self.no_cut_reason = "baselines_too_stale"
        elif msg.startswith("OD_REJECTED"):
            # args = (symbol, reason); reason is "<filter>: <detail>" or
            # "sized to zero".
            reason = str(args[1]) if len(args) > 1 else "unknown"
            key = reason.split(":")[0].strip()
            self.risk_rejects[key] = self.risk_rejects.get(key, 0) + 1
        elif msg.startswith("OD_TRIGGER_REJECTED_WIDE_STOP"):
            self.wide_stop_rejects += 1


# ─────────────────────────────────────────────────────────────────────────
# Point-in-time baselines
# ─────────────────────────────────────────────────────────────────────────


def point_in_time_baselines(
    source, symbols: Sequence[str], prev_session: date, *,
    or_minutes: int, lookback_sessions: int,
    cache_dir: Path | None = None,
) -> dict[str, OpeningDriveBaseline]:
    """Baselines exactly as the production 16:10 job would have built them
    on the previous session — i.e. knowing nothing about the test day.

    ``as_of`` is 16:10 NY on ``prev_session``, which is where
    ``main_opening_drive.refresh_baselines_post_close`` runs. Two
    consequences, both required for an honest backtest:

      * ``build_baselines`` derives its trailing session list with
        ``session_dates(..., now)``, which drops ``now``'s own date. From
        16:10 on the previous session that means the trailing OR-volume
        window ends the session BEFORE the previous one, and the test day is
        nowhere in it.
      * The daily bars it reads end at ``as_of``, so ATR(14) and ADV(20)
        include the previous session's completed daily bar and cannot see
        the test day at all.

    The committed ``runtime/opening_drive/baselines.json`` is never read:
    it holds a single snapshot computed today, which applied to a session 8
    months ago would leak eight months of future volume and volatility.
    """
    from scripts.build_opening_drive_baselines import build_baselines

    as_of = ny_dt(prev_session, 16, 10)
    if cache_dir is not None:
        cache_dir.mkdir(parents=True, exist_ok=True)
        path = cache_dir / f"{prev_session.isoformat()}.json"
        cached = load_baselines(path)
        if cached:
            return cached
    built = build_baselines(
        source, list(symbols), as_of,
        or_minutes=or_minutes, lookback_sessions=lookback_sessions,
    )
    if cache_dir is not None and built:
        save_baselines(built, cache_dir / f"{prev_session.isoformat()}.json")
    return built


# ─────────────────────────────────────────────────────────────────────────
# The backtest
# ─────────────────────────────────────────────────────────────────────────


@dataclass
class SessionOutcome:
    day: date
    qualifiers: int
    kept: int
    rejects: dict[str, int]
    no_cut_reason: str | None
    triggers: int
    entries: int
    trades: list[TradeRow]
    end_equity: float
    max_managed_bar_ts: datetime | None = None


@dataclass
class BacktestOutcome:
    sessions: list[SessionOutcome] = field(default_factory=list)
    trades: list[TradeRow] = field(default_factory=list)
    equity_curve: list[tuple[date, float]] = field(default_factory=list)
    start_equity: float = 0.0
    risk_rejects: dict[str, int] = field(default_factory=dict)
    wide_stop_rejects: int = 0
    unpriced_flattens: int = 0

    @property
    def rejects(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for s in self.sessions:
            for k, v in s.rejects.items():
                out[k] = out.get(k, 0) + v
        return out


class OpeningDriveBacktest:
    """Drives one production ``OpeningDriveLoop`` over a list of sessions."""

    def __init__(
        self,
        cfg: dict,
        source,
        universe: dict[str, str],
        equity: float,
        *,
        baselines_for: Callable[[date], dict[str, OpeningDriveBaseline]],
        screen_only: bool = False,
        side: str = "long",
        realistic_costs: bool = False,
    ) -> None:
        if side not in ("long", "short"):
            raise ValueError(f"side must be 'long' or 'short', got {side!r}")
        self.cfg = cfg
        self.source = source
        self.universe = universe
        self.screen_only = screen_only
        self.side = side
        self.realistic_costs = bool(realistic_costs)
        self.start_equity = float(equity)

        eq_raw = cfg["asset_classes"]["equity"]
        self.slippage_bps = float(eq_raw.get("slippage_bps", 0.0))
        self.commission_per_share = float(eq_raw.get("commission_per_share", 0.0))
        self.baselines_for = baselines_for

        self.eq_cfg = AssetClassConfig(
            name="equity", timezone=eq_raw["timezone"],
            session_open_local=eq_raw["session_open_local"],
            session_close_local=eq_raw["session_close_local"],
            opening_blackout_min=0,
            bar_timeframe=cfg["scheduler"]["bar_timeframe"],
            slippage_bps=self.slippage_bps,
            commission_per_share=self.commission_per_share,
            commission_bps=eq_raw.get("commission_bps", 0.0),
        )

        scan_cfg = cfg["scanner"]
        setup_cfg = cfg["setups"]["opening_drive"]
        self.scanner = OpeningDriveScanner(
            universe=universe,
            baselines={},
            filters=OpeningDriveFilters(**scan_cfg["filters"]),
            max_concurrent_positions=cfg["risk"]["max_concurrent_positions"],
            candidate_multiplier=scan_cfg["ranking"]["candidate_multiplier"],
            baselines_max_age_days=scan_cfg["baselines_max_age_days"],
            or_minutes=scan_cfg["or_minutes"],
        )
        self.od_cfg = OpeningDriveConfig(
            universe_path=scan_cfg["universe_file"],
            baselines_path=scan_cfg["baselines_path"],
            baselines_max_age_days=scan_cfg["baselines_max_age_days"],
            or_minutes=scan_cfg["or_minutes"],
            lookback_sessions=scan_cfg["lookback_sessions"],
            entry_window_minutes=setup_cfg["entry_window_minutes"],
            volume_confirm_mult=setup_cfg["volume_confirm_mult"],
            target_R=setup_cfg["target_R"],
            min_stop_atr_frac=setup_cfg["min_stop_atr_frac"],
            atr_mult_stop_cap=setup_cfg["atr_mult_stop_cap"],
            max_concurrent_positions=cfg["risk"]["max_concurrent_positions"],
            candidate_multiplier=scan_cfg["ranking"]["candidate_multiplier"],
            premarket_bar_timeframe=cfg["scheduler"]["bar_timeframe"],
            regular_bar_timeframe=cfg["scheduler"]["regular_session_timeframe"],
            side=side,
        )

        self.broker = BacktestBroker(self.slippage_bps)
        self.recorder = TradeRecorder(self.slippage_bps,
                                      self.commission_per_share,
                                      realistic_costs=self.realistic_costs)
        self.book = PositionBook()
        self.ledger = DailyLedger(initial_equity=self.start_equity)

        # Real production pipeline builder. alpaca/mysql are omitted, which
        # drops BrokerPositionFilter (fails open anyway) and
        # ManualCloseCooldownFilter (needs MySQL). Everything the spec calls
        # load-bearing — ConcurrentPositionFilter, SectorExposureFilter,
        # ConsecutiveLossFilter(system_wide), RiskBudgetFilter — is present.
        from main_opening_drive import build_pipeline
        pipeline = build_pipeline(cfg, sector_map=universe)
        sizing = SizingConfig(
            max_risk_per_trade=cfg["risk"]["max_risk_per_trade"],
            max_notional_per_trade_pct=cfg["risk"]["max_notional_per_trade_pct"],
            allow_fractional=False,
        )
        self.risk_manager = RiskManager(
            pipeline=pipeline, sizing_equity=sizing, sizing_crypto=sizing,
            ledger=self.ledger, book=self.book,
        )

        from broker.order_executor import OrderExecutor
        inner_exec = OrderExecutor(
            self.broker, self.book, strategy_name=cfg["system"]["name"],
            logger=logging.getLogger("backtest.executor"), mysql_store=None,
        )
        self.executor = RecordingExecutor(inner_exec, self.recorder,
                                         self.broker)

        pm_kwargs = dict(order_status_for=lambda pos: "filled")
        self.position_manager = PositionManager(
            self.book,
            max_hold_bars=cfg["position_management"]["max_hold_bars"],
            breakeven_at_R=cfg["position_management"]["breakeven_at_R"],
            **pm_kwargs,
        )
        # Entry-window manager. Live, the OCO bracket is resting at the
        # broker from the moment of entry, so a stop or target CAN fill
        # between 10:00 and 11:00. Ignoring that would silently discard
        # losers that recovered by 11:00. breakeven_at_R and max_hold_bars
        # are pushed out of reach because both are client-side actions the
        # production loop only performs in the managed phase, and spec 7.3
        # anchors the max_hold_bars clock at 11:00.
        self.entry_position_manager = PositionManager(
            self.book, max_hold_bars=10 ** 9, breakeven_at_R=10 ** 9,
            **pm_kwargs,
        )

        self.loop = OpeningDriveLoop(
            cfg=self.od_cfg, scanner=self.scanner,
            equity_asset_class=self.eq_cfg,
            alpaca_client=self.broker, alpaca_data=self.source,
            risk_manager=self.risk_manager, executor=self.executor,
            book=self.book, position_manager=self.position_manager,
            strategy_name=cfg["system"]["name"], mysql_store=None,
        )

        self.capture = CutLogCapture()
        self.capture.install()

    # ── helpers ─────────────────────────────────────────────────────────

    def _equity_now(self) -> float:
        """Authoritative equity: start capital plus realised NET P&L.

        Not ``ledger.equity``. ``record_exits_to_ledger`` adjusts the ledger
        for stop / target / time_stop only — the 15:30 flatten is not a
        ``PositionAction``, so it never reaches the ledger at all (true in
        production too, where the reconciler writes that close and the loop
        re-reads equity from the broker every minute). Left uncorrected, the
        sizing equity would silently omit every EOD-flat trade's P&L.
        """
        return self.start_equity + sum(r.pnl_net for r in self.recorder.rows)

    def _sync_equity_and_cash(self) -> None:
        """Mirror ``main_opening_drive.refresh_equity_and_cash``.

        available_cash is the backtest equity less the notional already
        committed, so ``size_position``'s cash cap is exercised rather than
        skipped. With max_notional_per_trade_pct 0.07 and 5 slots the cash
        cap never binds — spec 3.1's "the notional cap is the load-bearing
        limit" — but it must be wired, not omitted.
        """
        equity = self._equity_now()
        self.risk_manager.update_equity(equity)
        open_notional = sum(p.entry_px * p.qty for p in self.book.all())
        self.risk_manager.update_cash(max(0.0, equity - open_notional))

    def _filter_lt(self, bars_by_symbol: dict[str, list[Bar]],
                   end: datetime) -> dict[str, list[Bar]]:
        """The boundary rule: Alpaca's ``end`` is inclusive."""
        return {
            sym: [b for b in bars if b.ts < end]
            for sym, bars in bars_by_symbol.items()
        }

    # ── one session ─────────────────────────────────────────────────────

    def run_session(self, day: date) -> SessionOutcome:
        self.capture.reset()
        self.recorder.session = day
        self.recorder.scan_by_symbol = {}
        self.recorder.marks = {}
        self.recorder.live = {}
        self.recorder.stop_slip = {}
        self.recorder.current_bar = None
        self.loop.executor.reset_cycle()

        baselines = self.baselines_for(day)
        self.scanner.baselines = baselines

        cut_t = ny_dt(day, 10, 0)
        entry_end = ny_dt(day, 11, 0)
        eod_t = ny_dt(day, 15, 30)

        n_before = len(self.recorder.rows)
        self._sync_equity_and_cash()

        # The cut. run_cut is production code: it fetches the OR window,
        # applies the `b.ts < or_end` filter that keeps the 10:00 bar out of
        # the opening range, screens, ranks and builds the armed setups with
        # OR-seeded SessionContexts.
        watchlist = self.loop.run_cut(cut_t)
        for i, r in enumerate(watchlist):
            self.recorder.scan_by_symbol[r.symbol] = (r, i + 1)

        outcome = SessionOutcome(
            day=day, qualifiers=self.capture.qualifiers,
            kept=self.capture.kept, rejects=dict(self.capture.rejects),
            no_cut_reason=self.capture.no_cut_reason,
            triggers=0, entries=0, trades=[],
            end_equity=self._equity_now(),
        )
        if self.screen_only or not watchlist:
            self.loop.reset_for_new_day(eod_t)
            return outcome

        symbols = [r.symbol for r in watchlist]

        if self.realistic_costs:
            # Per-symbol stop-slippage budget for this session: 0.10 x the
            # mean 1-minute true range of that symbol's OWN opening range.
            # This re-reads the exact window run_cut just requested (same
            # timeframe/start/end, so the same CachedBarSource window), for a
            # subset of its symbols — a cache hit, never an extra API call.
            or_start, or_end = self.loop.or_window(cut_t)
            or_bars = self._filter_lt(
                self.source.get_bars_multi(symbols, "equity",
                                           self.od_cfg.premarket_bar_timeframe,
                                           or_start, or_end),
                or_end,
            )
            self.recorder.stop_slip = {
                sym: 0.10 * avg_minute_true_range(bars)
                for sym, bars in or_bars.items()
            }

        # ── entry window 10:00-11:00 on 1-minute bars ──────────────────
        entry_bars = self._filter_lt(
            self.source.get_bars_multi(
                symbols, "equity", self.od_cfg.premarket_bar_timeframe,
                cut_t, entry_end,
            ),
            entry_end,
        )
        for ts in sorted({b.ts for bars in entry_bars.values() for b in bars}):
            at_ts = {
                sym: b for sym, bars in entry_bars.items()
                for b in bars if b.ts == ts
            }
            self.recorder.current_ts = ts
            # Phase A: resting stop/target on already-open positions.
            for sym in symbols:
                bar = at_ts.get(sym)
                if bar is None or not self.book.get_all(sym):
                    continue
                self.recorder.marks[sym] = bar.close
                self.recorder.current_bar = bar
                snapshot = {p.setup: p for p in self.book.get_all(sym)}
                actions = self.entry_position_manager.on_bar(sym, bar)
                if actions:
                    record_exits_to_ledger(
                        self.ledger, sym, actions, bar,
                        positions_snapshot=snapshot, asset_class="equity",
                    )
                    self.executor.handle_actions(actions, asset_class="equity")
            # Phase B: triggers, in watchlist rank order within the minute.
            self._sync_equity_and_cash()
            for sym in symbols:
                bar = at_ts.get(sym)
                if bar is None:
                    continue
                self.recorder.marks[sym] = bar.close
                self.recorder.current_bar = bar
                # The trigger-bar entry penalty. Only the entry window sets a
                # non-zero extra; every other set_mark call clears it.
                self.broker.set_mark(
                    sym, bar.close,
                    extra_adverse=(0.05 * (bar.high - bar.low)
                                   if self.realistic_costs else 0.0),
                )
                setup = self.loop.day.setups.get(sym)
                before = setup.state if setup else None
                n_pos = self.book.count()
                self.loop.on_bar(sym, bar)
                if setup is not None and before == "ARMED" and setup.state == "FILLED":
                    outcome.triggers += 1
                    if self.book.count() > n_pos:
                        outcome.entries += 1

        # ── 11:00 boundary ─────────────────────────────────────────────
        self.loop.switch_to_regular_session_bars()
        # spec 7.3: max_hold_bars counts from the position's FIRST
        # managed-phase bar at 11:00, not from entry. The entry-window
        # manager above advanced bars_held; re-anchor here.
        for pos in self.book.all():
            pos.bars_held = 0

        held = sorted({p.symbol for p in self.book.all()})
        if held:
            managed = self._filter_lt(
                self.source.get_bars_multi(
                    held, "equity", self.od_cfg.regular_bar_timeframe,
                    entry_end, eod_t,
                ),
                eod_t,
            )
            for ts in sorted({b.ts for bars in managed.values() for b in bars}):
                self.recorder.current_ts = ts
                for pos in list(self.book.all()):
                    bar = next(
                        (b for b in managed.get(pos.symbol, []) if b.ts == ts),
                        None,
                    )
                    if bar is None:
                        continue
                    self.recorder.marks[pos.symbol] = bar.close
                    self.recorder.current_bar = bar
                    self.broker.set_mark(pos.symbol, bar.close)
                    self.loop.manage_open(pos.symbol, bar)

        # ── 15:30 unconditional flatten ────────────────────────────────
        # The mark is the close of the last bar that ENDS at or before
        # 15:30 (bar timestamps are bar-open), so nothing after the flatten
        # decision informs its price.
        self.recorder.current_ts = eod_t
        # The flatten is priced off recorder.marks, not off a bar; clear the
        # bar so no stale one can influence a fill.
        self.recorder.current_bar = None
        self.loop.force_close_all(eod_t)
        # Anything the flatten could not price (no bars at all) must not be
        # left silently open in the recorder's view.
        for (sym, setup) in list(self.recorder.live):
            self.recorder.unpriced_flattens += 1
            logger.warning("BT_UNPRICED_FLATTEN symbol=%s session=%s",
                           sym, day)
            self.recorder.live.pop((sym, setup), None)

        outcome.trades = self.recorder.rows[n_before:]
        seen = list(self.loop.day.last_managed_bar_ts.values())
        outcome.max_managed_bar_ts = max(seen) if seen else None
        self.loop.reset_for_new_day(eod_t)
        outcome.end_equity = self._equity_now()
        return outcome

    def run(self, sessions: Iterable[date]) -> BacktestOutcome:
        out = BacktestOutcome(start_equity=self.start_equity)
        for day in sessions:
            res = self.run_session(day)
            out.sessions.append(res)
            out.trades.extend(res.trades)
            out.equity_curve.append((day, res.end_equity))
            logger.info(
                "BT_SESSION day=%s qualifiers=%d kept=%d triggers=%d "
                "entries=%d trades=%d equity=%.2f",
                day, res.qualifiers, res.kept, res.triggers, res.entries,
                len(res.trades), res.end_equity,
            )
        out.risk_rejects = dict(self.capture.risk_rejects)
        out.wide_stop_rejects = self.capture.wide_stop_rejects
        out.unpriced_flattens = self.recorder.unpriced_flattens
        return out


# ─────────────────────────────────────────────────────────────────────────
# Reporting
# ─────────────────────────────────────────────────────────────────────────


def trades_dataframe(rows: Sequence[TradeRow]) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame(columns=["pnl_usd", "R_realized"])
    return pd.DataFrame([
        {**vars(r), "pnl_usd": r.pnl_net, "R_realized": r.R_net}
        for r in rows
    ])


def summarize(out: BacktestOutcome) -> dict:
    """Headline figures. Reuses backtest/performance.compute_metrics for the
    parts that fit (win rate, profit factor, avg R, max drawdown) and adds
    the ones it does not carry."""
    from backtest.performance import compute_metrics

    trades = trades_dataframe(out.trades)
    curve = pd.Series(
        [e for _, e in out.equity_curve],
        index=pd.to_datetime([d for d, _ in out.equity_curve]),
    ) if out.equity_curve else pd.Series(dtype=float)
    base = compute_metrics(curve, trades)
    # compute_metrics annualizes Sharpe assuming a 5-minute bar equity
    # series; this curve is daily, so the number would be wrong. Dropped
    # rather than reported misleadingly.
    base.pop("sharpe", None)

    n_sessions = len(out.sessions)
    kept = [s.kept for s in out.sessions]
    quals = [s.qualifiers for s in out.sessions]
    r_net = [r.R_net for r in out.trades]
    end_equity = out.equity_curve[-1][1] if out.equity_curve else out.start_equity
    reasons = {k: 0 for k in EXIT_REASONS}
    for r in out.trades:
        reasons[r.exit_reason] = reasons.get(r.exit_reason, 0) + 1
    se_mean_R = (
        statistics.stdev(r_net) / (len(r_net) ** 0.5) if len(r_net) > 1
        else 0.0
    )

    return {
        **base,
        "sessions": n_sessions,
        "sessions_firing": sum(1 for k in kept if k > 0),
        "pct_sessions_firing": (
            sum(1 for k in kept if k > 0) / n_sessions if n_sessions else 0.0
        ),
        "candidates_per_day": (sum(kept) / n_sessions) if n_sessions else 0.0,
        "qualifiers_per_day": (sum(quals) / n_sessions) if n_sessions else 0.0,
        "triggers": sum(s.triggers for s in out.sessions),
        "entries": sum(s.entries for s in out.sessions),
        "median_R": statistics.median(r_net) if r_net else 0.0,
        "mean_R": statistics.fmean(r_net) if r_net else 0.0,
        # Standard error of the mean R and its t-statistic against zero. This
        # is the number the pre-registered decision rule is stated in, so it
        # is computed here rather than in a notebook: sd / sqrt(n), with the
        # sample (n-1) standard deviation.
        "sd_R": statistics.stdev(r_net) if len(r_net) > 1 else 0.0,
        "se_mean_R": se_mean_R,
        "t_mean_R": (
            statistics.fmean(r_net) / se_mean_R if se_mean_R > 0 else 0.0
        ),
        "total_return_pct": (
            (end_equity / out.start_equity - 1.0) * 100.0
            if out.start_equity else 0.0
        ),
        "start_equity": out.start_equity,
        "end_equity": end_equity,
        "net_pnl": end_equity - out.start_equity,
        "avg_hold_minutes": (
            statistics.fmean([r.hold_minutes for r in out.trades])
            if out.trades else 0.0
        ),
        "exit_reasons": reasons,
        "rejects": out.rejects,
        "risk_rejects": out.risk_rejects,
        "wide_stop_rejects": out.wide_stop_rejects,
        "unpriced_flattens": out.unpriced_flattens,
    }


def print_summary(label: str, summary: dict, equity: float,
                  screen_only: bool) -> None:
    print()
    print("=" * 72)
    print(f"OPENING DRIVE BACKTEST — {label}")
    print("=" * 72)
    print(f"  backtest equity used     : ${equity:,.2f}")
    print(f"  sessions                 : {summary['sessions']}")
    print(f"  sessions firing          : {summary['sessions_firing']} "
          f"({summary['pct_sessions_firing'] * 100:.1f}%)")
    print(f"  candidates/day (kept)    : {summary['candidates_per_day']:.2f}")
    print(f"  qualifiers/day (pre-topN): {summary['qualifiers_per_day']:.2f}")
    if not screen_only:
        print(f"  triggers / entries       : {summary['triggers']} / "
              f"{summary['entries']}")
        print(f"  trades                   : {summary['trades']}")
        print(f"  win rate                 : {summary['win_rate'] * 100:.1f}%")
        print(f"  mean R (net)             : {summary['mean_R']:+.3f}"
              f"  (se {summary['se_mean_R']:.3f}, "
              f"t {summary['t_mean_R']:+.2f})")
        print(f"  median R (net)           : {summary['median_R']:+.3f}")
        print(f"  total return             : {summary['total_return_pct']:+.2f}%"
              f"  (${summary['net_pnl']:+,.2f})")
        print(f"  max drawdown             : "
              f"{summary['max_drawdown'] * 100:.2f}%")
        print(f"  profit factor            : {summary['profit_factor']:.3f}")
        print(f"  avg holding time         : "
              f"{summary['avg_hold_minutes']:.1f} min")
        print("  exit reasons             : " + ", ".join(
            f"{k}={v}" for k, v in summary["exit_reasons"].items()
        ))
    print("  per-gate reject histogram (symbol-days):")
    total_rej = sum(summary["rejects"].values()) or 1
    for k, v in sorted(summary["rejects"].items(), key=lambda kv: -kv[1]):
        print(f"      {k:<24} {v:>9,}  ({v / total_rej * 100:5.2f}%)")
    if not screen_only:
        print(f"  wide-stop trigger rejects: {summary['wide_stop_rejects']}")
        print("  post-trigger risk rejects:")
        for k, v in sorted(summary["risk_rejects"].items(),
                           key=lambda kv: -kv[1]):
            print(f"      {k:<24} {v:>9,}")
        if summary["unpriced_flattens"]:
            print(f"  UNPRICED FLATTENS        : "
                  f"{summary['unpriced_flattens']}")
    print("=" * 72)


def write_trades_csv(rows: Sequence[TradeRow], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = list(vars(rows[0]).keys()) if rows else [
        f for f in TradeRow.__dataclass_fields__
    ]
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow(vars(r))


# ─────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────


def apply_overrides(cfg: dict, overrides: Sequence[str]) -> dict:
    """Apply repeatable ``--set a.b.c=value`` overrides in place."""
    for item in overrides:
        if "=" not in item:
            raise SystemExit(f"--set expects key=value, got {item!r}")
        dotted, raw = item.split("=", 1)
        node = cfg
        parts = dotted.split(".")
        for p in parts[:-1]:
            if p not in node or not isinstance(node[p], dict):
                raise SystemExit(f"--set: no config section {dotted!r}")
            node = node[p]
        if parts[-1] not in node:
            raise SystemExit(f"--set: unknown config key {dotted!r}")
        node[parts[-1]] = yaml.safe_load(raw)
        logger.info("BT_OVERRIDE %s=%r", dotted, node[parts[-1]])
    return cfg


def session_dates_from(source, start: date, end: date) -> list[date]:
    """Trading sessions in [start, end], taken from SPY's daily bars.

    SPY prints every open session, so its daily bars ARE the calendar — the
    same trick ``build_opening_drive_baselines.session_dates`` uses, and it
    cannot get holidays wrong.
    """
    bars = source.get_bars_multi(
        ["SPY"], "equity", "1Day",
        ny_dt(start, 0, 0) - timedelta(days=5), ny_dt(end, 23, 0),
    ).get("SPY") or []
    return sorted({
        ny_date(b.ts) for b in bars if start <= ny_date(b.ts) <= end
    })


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config",
                    default="config/settings_opening_drive_equity.yaml")
    ap.add_argument("--start", required=True, help="YYYY-MM-DD")
    ap.add_argument("--end", required=True, help="YYYY-MM-DD")
    ap.add_argument("--equity", type=float, default=100_000.0,
                    help="backtest equity; the live account is NOT read")
    ap.add_argument("--screen-only", action="store_true")
    ap.add_argument("--side", choices=("long", "short"), default="long",
                    help="which way to trade the trigger. Detection is "
                         "identical either way; only stop/target placement "
                         "mirrors. Default long — existing behaviour.")
    ap.add_argument("--realistic-costs", action="store_true",
                    help="entry pays 0.05 x trigger-bar range on top of "
                         "slippage_bps; stops slip 0.10 x the mean 1-minute "
                         "true range of the opening range and fill at the "
                         "bar open when it gapped through. Short borrow is "
                         "modelled as zero (intraday only).")
    ap.add_argument("--set", action="append", default=[], dest="overrides",
                    metavar="KEY=VALUE")
    ap.add_argument("--cache-dir", default=DEFAULT_CACHE_DIR)
    ap.add_argument("--out-dir", default=DEFAULT_OUT_DIR)
    ap.add_argument("--tag", default=None,
                    help="suffix for the per-trade CSV filename")
    ap.add_argument("--no-fetch", action="store_true",
                    help="fail instead of hitting the API on a cache miss")
    ap.add_argument("--log-level", default="WARNING")
    args = ap.parse_args(argv)

    level = getattr(logging, args.log_level.upper(), logging.WARNING)
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )
    # The tapped production loggers are raised to INFO so the reject
    # histograms can be read off their records. Gate the CONSOLE at the
    # handler instead, or every expired setup lands on stderr.
    for h in logging.getLogger().handlers:
        h.setLevel(level)
    # This harness's own progress lines always show.
    logger.setLevel(logging.INFO)
    logger.propagate = False
    _own = logging.StreamHandler(sys.stderr)
    _own.setLevel(logging.INFO)
    _own.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    logger.addHandler(_own)

    with open(args.config) as f:
        cfg = yaml.safe_load(f)
    apply_overrides(cfg, args.overrides)

    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)

    from broker.alpaca_client import AlpacaClient
    client = AlpacaClient(asset_class="equity")
    source = CachedBarSource(client, args.cache_dir,
                             allow_fetch=not args.no_fetch)

    universe = load_universe(cfg["scanner"]["universe_file"])
    symbols = sorted(universe) + [OpeningDriveScanner.BENCHMARK]

    # One daily fetch for the whole run. build_baselines wants 60 days of
    # history before its as_of, so reach back 150 calendar days.
    source.prime_daily(
        symbols,
        ny_dt(start, 0, 0) - timedelta(days=150),
        ny_dt(end, 23, 0),
    )
    sessions = session_dates_from(source, start, end)
    if not sessions:
        raise SystemExit("no trading sessions in range")
    print(f"sessions: {len(sessions)}  ({sessions[0]} .. {sessions[-1]})")
    print(f"equity used: ${args.equity:,.2f}")
    print(f"side: {args.side}   cost model: "
          f"{'realistic' if args.realistic_costs else 'optimistic'}")

    prev_of: dict[date, date] = {}
    all_days = sorted({
        ny_date(b.ts)
        for b in (source.get_bars_multi(
            ["SPY"], "equity", "1Day",
            ny_dt(start, 0, 0) - timedelta(days=150), ny_dt(end, 23, 0),
        ).get("SPY") or [])
    })
    for i, d in enumerate(all_days):
        if i > 0:
            prev_of[d] = all_days[i - 1]

    baseline_cache = Path(args.cache_dir) / "baselines"
    scan_cfg = cfg["scanner"]

    def baselines_for(day: date) -> dict[str, OpeningDriveBaseline]:
        prev = prev_of.get(day)
        if prev is None:
            return {}
        return point_in_time_baselines(
            source, symbols, prev,
            or_minutes=scan_cfg["or_minutes"],
            lookback_sessions=scan_cfg["lookback_sessions"],
            cache_dir=baseline_cache,
        )

    bt = OpeningDriveBacktest(
        cfg, source, universe, args.equity,
        baselines_for=baselines_for, screen_only=args.screen_only,
        side=args.side, realistic_costs=args.realistic_costs,
    )
    out = bt.run(sessions)
    summary = summarize(out)
    summary["side"] = args.side
    summary["realistic_costs"] = args.realistic_costs

    label = args.tag or (
        f"{args.start}_{args.end}_{args.side}"
        + ("_realistic" if args.realistic_costs else "_optimistic")
        + ("_screen" if args.screen_only else "")
    )
    print_summary(label, summary, args.equity, args.screen_only)
    print(f"api window fetches: {source.api_fetches}   "
          f"cache window reads: {source.cache_reads}")

    if not args.screen_only:
        csv_path = Path(args.out_dir) / f"trades_{label}.csv"
        write_trades_csv(out.trades, csv_path)
        print(f"per-trade CSV: {csv_path}")
    json_path = Path(args.out_dir) / f"summary_{label}.json"
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(summary, indent=2, default=str))
    print(f"summary JSON : {json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
