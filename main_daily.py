"""SMA-Slope daily trend-following bot — long-only, TQQQ, 1Day timeframe.

Dedicated entry point. The intraday engine (`main.py` + `VWAPWaveEngine` +
`SessionContext`) is built around per-session bars and Min/Hour timeframes:
SessionContext resets its bar history every session, and the scheduler regex
only accepts Min/Hour. A daily SMA(250) trend follower needs multi-month
rolling history and once-per-day evaluation, so it gets its own loop (same
pattern as `main_gap_and_go.py`, which also bypasses the intraday engine).

Strategy (faithful to scripts/sma_slope_backtest.py, no lookahead):
    SMA(N); slope(t) = SMA(t) - SMA(t-k).
    Entry (long): close(t) > SMA(t) AND slope(t) > 0  -> buy at open of t+1.
    Exit:         close(t) < SMA(t) OR  slope(t) < 0  -> sell at open of t+1.

Operational model
-----------------
The bot wakes once per trading day, just after the NYSE open. At that point
the *previous* completed daily bar (day t) is fully available, so it computes
the signal on day t's close and executes at today's open (t+1) — mathematically
identical to "signal at close t, execute at open t+1", but with synchronous
market orders (no overnight queueing, no premature position logging).

Protective stop/target: the system has a hard "no naked positions" rule and the
reflection loop needs initial_stop_px to compute R-multiples, so every position
is opened with an ATR-based protective stop and a far target. These are safety
floors — the SMA flip is the primary exit.
"""

from __future__ import annotations

import logging
import signal as _signal
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import pytz
import yaml

import pandas_market_calendars as mcal

from broker.alpaca_client import AlpacaClient
from broker.alpaca_data import AlpacaData
from broker.client_order_id import Role, make_client_order_id
from core.atr import atr as compute_atr
from core.bar import Bar
from notifications import send_position_open_alert
from state.mysql_store import MySQLStore
from state.position_book import OpenPosition, PositionBook
from ui.logging_setup import setup_logging

logger = logging.getLogger("sma_slope")

_NY_TZ = pytz.timezone("America/New_York")
_SETUP = "sma_slope"
_HISTORY_LOOKBACK_DAYS = 1500   # calendar days to pull at warmup (covers N=250 comfortably)
_shutdown = False


def _handle_shutdown(signum, frame):
    global _shutdown
    _shutdown = True


def load_config(path: str) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


# ---------------------------------------------------------------------------
# Market calendar helpers
# ---------------------------------------------------------------------------

def next_market_open_utc(after_utc: datetime, nyse) -> datetime:
    """Return the next NYSE market-open (UTC) strictly after `after_utc`."""
    window_end = after_utc + timedelta(days=14)
    schedule = nyse.schedule(start_date=after_utc.date(), end_date=window_end.date())
    for _, row in schedule.iterrows():
        open_utc = row["market_open"].to_pydatetime()
        if open_utc > after_utc:
            return open_utc
    raise RuntimeError("No market open found in the next 14 days")


def today_midnight_et_utc(now_utc: datetime) -> datetime:
    """Return today's 00:00 ET as UTC — the exclusive upper bound for
    completed daily bars (Alpaca stamps a daily bar at 00:00 ET of its day,
    so `end` == today-00:00 ET excludes today's in-progress bar)."""
    today = now_utc.astimezone(_NY_TZ).date()
    midnight_et = _NY_TZ.localize(datetime.combine(today, datetime.min.time()))
    return midnight_et.astimezone(pytz.UTC)


# ---------------------------------------------------------------------------
# Signal computation
# ---------------------------------------------------------------------------

def compute_signal(closes: list[float], period: int, slope_lookback: int) -> tuple[bool, float, float, float]:
    """Return (long_signal, sma, slope, close_t) from a rolling list of closes.

    `closes[-1]` is bar t. Uses a strict causal window (no future bars).
    Returns (False, 0, 0, 0) when insufficient history.
    """
    if len(closes) < period + slope_lookback:
        return False, 0.0, 0.0, 0.0
    close_t = closes[-1]
    sma = sum(closes[-period:]) / period
    sma_km1 = sum(closes[-period - slope_lookback:-slope_lookback]) / period
    slope = sma - sma_km1
    long_signal = close_t > sma and slope > 0
    return long_signal, sma, slope, close_t


# ---------------------------------------------------------------------------
# Order helpers
# ---------------------------------------------------------------------------

def _submit_market(alpaca: AlpacaClient, symbol: str, qty: int, side: str,
                   role: str, strategy_name: str) -> dict:
    """Submit a market order and return the Alpaca order dict."""
    coid = make_client_order_id(strategy_name, _SETUP, symbol, role)
    return alpaca.submit_order(
        symbol=symbol,
        qty=qty,
        side=side,
        order_type="market",
        time_in_force="day",
        client_order_id=coid,
    )


def _fill_price(order: dict, fallback: float) -> float:
    fp = float((order or {}).get("filled_avg_price") or 0)
    return fp if fp > 0 else fallback


# ---------------------------------------------------------------------------
# The daily bot
# ---------------------------------------------------------------------------

class SmaSlopeDailyBot:
    def __init__(self, cfg: dict, logger_: logging.Logger):
        self.cfg = cfg
        self.logger = logger_
        self.symbol = cfg["asset_classes"]["equity"]["symbols"][0]
        self.setup_cfg = cfg["setups"][_SETUP]
        self.period = int(self.setup_cfg["period"])
        self.slope_lookback = int(self.setup_cfg["slope_lookback"])
        self.atr_mult_stop = float(self.setup_cfg["atr_mult_stop"])
        self.target_R = float(self.setup_cfg["target_R"])
        self.notional_pct = float(cfg["risk"]["max_notional_per_trade_pct"])
        self.history_bars = int(cfg["scheduler"]["history_bars"])
        self.grace = int(cfg["scheduler"]["wake_grace_seconds"])
        self.strategy_name = cfg["system"]["name"]

        self.nyse = mcal.get_calendar("NYSE")
        self.alpaca = AlpacaClient(asset_class="equity")
        self.data = AlpacaData(self.alpaca, cache_dir=cfg["backtest"]["cache_dir"])

        self.mysql = MySQLStore(strategy_name=self.strategy_name, logger=logger_)
        self.mysql.ensure_schema()
        self.mysql.upsert_strategy()

        self.book: PositionBook = self.mysql.load_open_positions()
        self.history: list[Bar] = []

    # -- setup --------------------------------------------------------------

    @staticmethod
    def _completed_only(bars: list[Bar], now_utc: datetime) -> list[Bar]:
        """Drop today's in-progress daily bar (Alpaca stamps a daily bar at
        00:00 ET of its day, and the `end` boundary is inclusive, so a fetch
        that ends at today-00:00 ET still returns today's partial bar). Only
        bars whose NY-local date is strictly before today are complete."""
        today = now_utc.astimezone(_NY_TZ).date()
        return [b for b in bars if b.ts.astimezone(_NY_TZ).date() < today]

    def warmup(self) -> None:
        """Load `history_bars` of completed daily bars for the symbol."""
        now = datetime.now(timezone.utc)
        end = today_midnight_et_utc(now)
        start = end - timedelta(days=_HISTORY_LOOKBACK_DAYS)
        bars = self.data.get_bars(self.symbol, "equity", "1Day",
                                  start=start, end=end, use_cache=True)
        bars = self._completed_only(bars, now)
        self.history = bars[-self.history_bars:]
        self.logger.info(
            "SMA_SLOPE_WARMUP symbol=%s daily_bars=%d last_ts=%s",
            self.symbol, len(self.history),
            self.history[-1].ts if self.history else "none",
        )

    def _refresh_history(self, now_utc: datetime) -> None:
        """Append any newly completed daily bars to the rolling history."""
        end = today_midnight_et_utc(now_utc)
        last_ts = self.history[-1].ts if self.history else None
        start = last_ts if last_ts is not None else end - timedelta(days=7)
        try:
            bars = self.data.get_bars(self.symbol, "equity", "1Day",
                                      start=start, end=end, use_cache=False)
        except Exception as exc:
            self.logger.error("SMA_SLOPE_BARS_FAILED symbol=%s error=%s",
                              self.symbol, exc)
            return
        bars = self._completed_only(bars, now_utc)
        known = {b.ts for b in self.history}
        for b in bars:
            if b.ts not in known:
                self.history.append(b)
        # Trim to rolling window.
        if len(self.history) > self.history_bars:
            self.history = self.history[-self.history_bars:]

    # -- position state -----------------------------------------------------

    def _position(self) -> Optional[OpenPosition]:
        return self.book.get(self.symbol, _SETUP)

    def _account_equity(self) -> float:
        acct = self.alpaca.get_account()
        eq = float(acct.get("equity") or acct.get("portfolio_value") or 0)
        if eq <= 0:
            raise RuntimeError("Account returned non-positive equity")
        return eq

    # -- actions ------------------------------------------------------------

    def _enter(self, bar_t: Bar, atr: float) -> None:
        equity = self._account_equity()
        ref_price = bar_t.close
        qty = int(equity * self.notional_pct // ref_price)
        if qty < 1:
            self.logger.info("SMA_SLOPE_SIZED_ZERO equity=%.2f ref_price=%.2f",
                             equity, ref_price)
            return
        order = _submit_market(self.alpaca, self.symbol, qty, "buy",
                               Role.ENTRY, self.strategy_name)
        entry = _fill_price(order, ref_price)
        stop_dist = self.atr_mult_stop * atr if atr > 0 else entry * 0.10
        stop = entry - stop_dist
        target = entry + self.target_R * stop_dist

        pos = OpenPosition(
            symbol=self.symbol, setup=_SETUP, side="long",
            qty=qty, entry_px=entry, stop_px=stop, target_px=target,
            opened_at=datetime.now(timezone.utc),
            order_id=(order or {}).get("id", ""),
            initial_stop_px=stop,
            client_order_id=(order or {}).get("client_order_id"),
            fill_confirmed=True,
        )
        self.book.add(pos)
        try:
            self.mysql.position_opened(pos, "equity")
        except Exception as exc:
            self.logger.error("SMA_SLOPE_MYSQL_OPEN_FAILED error=%s", exc)
        send_position_open_alert(
            strategy_name=self.strategy_name, symbol=self.symbol,
            side="long", qty=qty, entry_px=entry, stop_px=stop,
            target_px=target, setup_name=_SETUP, asset_class="equity",
        )
        self.logger.info(
            "SMA_SLOPE_ENTER symbol=%s qty=%d entry=%.4f stop=%.4f target=%.4f sma=%s",
            self.symbol, qty, entry, stop, target, "n/a",
        )

    def _exit(self, pos: OpenPosition, reason: str) -> None:
        order = _submit_market(self.alpaca, self.symbol, int(pos.qty), "sell",
                               Role.EXIT, self.strategy_name)
        fill = _fill_price(order, pos.entry_px)
        try:
            self.mysql.position_closed(
                self.symbol, fill, reason,
                setup_name=_SETUP,
                exit_client_order_id=(order or {}).get("client_order_id"),
            )
        except Exception as exc:
            self.logger.error("SMA_SLOPE_MYSQL_CLOSE_FAILED error=%s", exc)
        self.book.close(self.symbol, _SETUP)
        self.logger.info(
            "SMA_SLOPE_EXIT symbol=%s reason=%s exit=%.4f entry=%.4f",
            self.symbol, reason, fill, pos.entry_px,
        )

    # -- daily cycle --------------------------------------------------------

    def run_cycle(self, now_utc: datetime) -> None:
        self._refresh_history(now_utc)
        if len(self.history) < self.period + self.slope_lookback:
            self.logger.info("SMA_SLOPE_INSUFFICIENT_HISTORY bars=%d",
                             len(self.history))
            return

        closes = [b.close for b in self.history]
        bar_t = self.history[-1]
        atr = compute_atr(self.history, 14)
        long_signal, sma, slope, close_t = compute_signal(
            closes, self.period, self.slope_lookback)

        # Rebuild book from MySQL (external closes / reconciler adoptions).
        try:
            fresh = self.mysql.load_open_positions()
            self.book.replace_from(fresh)
        except Exception as exc:
            self.logger.error("SMA_SLOPE_REBUILD_FAILED error=%s", exc)

        pos = self._position()

        # 1) Manage an existing position (exit checks on completed bar t).
        if pos is not None and pos.side == "long":
            if pos.stop_px is not None and bar_t.low <= pos.stop_px:
                self._exit(pos, "stop")
            elif pos.target_px is not None and bar_t.high >= pos.target_px:
                self._exit(pos, "target")
            elif not long_signal:
                self._exit(pos, "sma_flip")

        # 2) Entry (only if flat after any exit this cycle).
        if self._position() is None and long_signal:
            self._enter(bar_t, atr)

        self.logger.info(
            "SMA_SLOPE_CYCLE close=%.4f sma=%.4f slope=%.4f long=%s open=%s",
            close_t, sma, slope, long_signal, self._position() is not None,
        )

    def run(self) -> None:
        self.warmup()
        self.logger.info("SMA_SLOPE_BOT_STARTED symbol=%s period=%d slope_k=%d",
                         self.symbol, self.period, self.slope_lookback)
        while not _shutdown:
            try:
                now = datetime.now(timezone.utc)
                next_open = next_market_open_utc(now, self.nyse)
                target = next_open + timedelta(seconds=self.grace)
                wait = (target - now).total_seconds()
                if wait > 0:
                    self.logger.info("SMA_SLOPE_SLEEP until=%s", target.isoformat())
                    # Sleep in small slices so shutdown stays responsive.
                    while not _shutdown and datetime.now(timezone.utc) < target:
                        time.sleep(min(60.0, (target - datetime.now(timezone.utc)).total_seconds()))
                    if _shutdown:
                        break
                self.run_cycle(datetime.now(timezone.utc))
            except Exception as exc:
                self.logger.error("SMA_SLOPE_LOOP_ERROR: %s", exc, exc_info=True)
                time.sleep(60)
        self.logger.info("SMA_SLOPE_SHUTDOWN")


def main() -> None:
    if "--config" not in sys.argv:
        raise SystemExit("main_daily.py requires --config <yaml>")
    idx = sys.argv.index("--config")
    if idx + 1 >= len(sys.argv):
        raise SystemExit("--config requires a path argument")
    cfg = load_config(sys.argv[idx + 1])
    system_name = cfg["system"]["name"]
    logger_ = setup_logging(log_file=cfg["logging"]["log_file"],
                            logger_name=system_name)
    logger_.info("%s starting up; env=%s", system_name, cfg["system"]["trading_env"])

    _signal.signal(_signal.SIGTERM, _handle_shutdown)
    _signal.signal(_signal.SIGINT, _handle_shutdown)

    bot = SmaSlopeDailyBot(cfg, logger_)
    bot.run()


if __name__ == "__main__":
    main()
