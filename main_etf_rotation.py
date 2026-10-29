"""Dual-Momentum Tactical ETF Rotation Strategy Bot.

Long-only tactical rotation across tradeable UCITS ETFs (or US equivalents)
with a 200-day SMA macro regime filter, CBOE VIX volatility circuit breaker,
and turnover buffer rules.

Operational Execution:
- Trimester Rebalance: Last Friday of March, June, September, and December.
- Weekly Risk-Off Check: Every Friday at 3:45 PM EST (or close evaluation).
- Re-Entry Hysteresis Rule: Requires 3 consecutive closes above 200d SMA AND VIX < 25.
- Order Execution: Limit orders placed at the bid/ask midpoint 15-30 minutes after open (10:00 AM EST).
"""

from __future__ import annotations

import logging
import signal as _signal
import sys
import time
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import pandas as pd
import pytz
import yaml

import pandas_market_calendars as mcal

from broker.alpaca_client import AlpacaClient
from broker.alpaca_data import AlpacaData
from broker.client_order_id import Role, make_client_order_id
from core.bar import Bar
from notifications import send_position_open_alert
from state.mysql_store import MySQLStore
from state.position_book import OpenPosition, PositionBook
from strategies.dual_momentum_etf import (
    DualMomentumConfig,
    DualMomentumEngine,
    RegimeState,
    RebalanceTrigger,
    compute_sma_200,
    is_friday,
    is_trimester_rebalance_date,
)
from ui.logging_setup import setup_logging

logger = logging.getLogger("etf_rotation")

_NY_TZ = pytz.timezone("America/New_York")
_SETUP = "dual_momentum_etf"
_shutdown = False


def _handle_shutdown(signum, frame):
    global _shutdown
    _shutdown = True


def load_config(path: str) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


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
    """Return today's 00:00 ET as UTC — the exclusive upper bound for completed daily bars."""
    today = now_utc.astimezone(_NY_TZ).date()
    midnight_et = _NY_TZ.localize(datetime.combine(today, datetime.min.time()))
    return midnight_et.astimezone(pytz.UTC)


def _fill_price(order: dict, fallback: float) -> float:
    fp = float((order or {}).get("filled_avg_price") or 0)
    return fp if fp > 0 else fallback


class EtfRotationBot:
    """Production bot for Dual-Momentum Tactical ETF Rotation Strategy."""

    def __init__(self, cfg: dict, logger_: logging.Logger, alpaca: Optional[AlpacaClient] = None, mysql: Optional[MySQLStore] = None):
        self.cfg = cfg
        self.logger = logger_
        self.strategy_name = cfg["system"]["name"]
        setup_cfg = cfg["setups"][_SETUP]
        uni_cfg = cfg.get("universe", {})

        # Strategy parameters
        self.satellites = uni_cfg.get("satellites", [
            "SMGB", "QDVE", "EQQQ", "ISPY", "XUFN", "SXLE", "XUIN"
        ])
        self.safe_harbor = uni_cfg.get("safe_harbor", "CSH2")
        self.benchmark = uni_cfg.get("benchmark", "SPY")
        self.vix_symbol = uni_cfg.get("vix", "VIX")

        self.dm_cfg = DualMomentumConfig(
            lookback_12w=int(setup_cfg.get("lookback_12w", 60)),
            lookback_24w=int(setup_cfg.get("lookback_24w", 120)),
            weight_12w=float(setup_cfg.get("weight_12w", 0.60)),
            weight_24w=float(setup_cfg.get("weight_24w", 0.40)),
            sma_period=int(setup_cfg.get("sma_period", 200)),
            vix_defensive_threshold=float(setup_cfg.get("vix_defensive_threshold", 22.0)),
            vix_crisis_threshold=float(setup_cfg.get("vix_crisis_threshold", 32.0)),
            vix_reentry_threshold=float(setup_cfg.get("vix_reentry_threshold", 25.0)),
            consecutive_below_sma_exit=int(setup_cfg.get("consecutive_below_sma_exit", 2)),
            consecutive_above_sma_reenter=int(setup_cfg.get("consecutive_above_sma_reenter", 3)),
            turnover_buffer_threshold=float(setup_cfg.get("turnover_buffer_threshold", 0.01)),
            satellite_symbols=self.satellites,
            safe_harbor_symbol=self.safe_harbor,
            benchmark_symbol=self.benchmark,
            vix_symbol=self.vix_symbol,
            rebalance_months=tuple(setup_cfg.get("rebalance_months", [3, 6, 9, 12])),
        )
        self.engine = DualMomentumEngine(self.dm_cfg)

        self.history_bars = int(cfg.get("scheduler", {}).get("history_bars", 300))
        self.execution_minute_offset = int(cfg.get("scheduler", {}).get("execution_minute_offset", 30))
        self.grace = int(cfg.get("scheduler", {}).get("wake_grace_seconds", 60))

        self.nyse = mcal.get_calendar("NYSE")
        self.alpaca = alpaca or AlpacaClient(asset_class="equity")
        self.data = AlpacaData(self.alpaca, cache_dir=cfg.get("backtest", {}).get("cache_dir", "runtime/bars_cache"))

        self.mysql = mysql or MySQLStore(strategy_name=self.strategy_name, logger=logger_)
        self.mysql.ensure_schema()
        self.mysql.upsert_strategy()

        self.book: PositionBook = self.mysql.load_open_positions()

        # Rolling history per symbol: symbol -> list[Bar]
        self.history: dict[str, list[Bar]] = {}

    @staticmethod
    def _completed_only(bars: list[Bar], now_utc: datetime) -> list[Bar]:
        today = now_utc.astimezone(_NY_TZ).date()
        return [b for b in bars if b.ts.astimezone(_NY_TZ).date() < today]

    def _load_symbol_history(self, symbol: str, now_utc: datetime, days: int = 1500) -> list[Bar]:
        end = today_midnight_et_utc(now_utc)
        start = end - timedelta(days=days)
        try:
            bars = self.data.get_bars(symbol, "equity", "1Day", start=start, end=end, use_cache=True)
            completed = self._completed_only(bars, now_utc)
            return completed[-self.history_bars:]
        except Exception as exc:
            self.logger.warning("ETF_ROTATION_FETCH_FAILED symbol=%s error=%s", symbol, exc)
            return []

    def warmup(self, now_utc: Optional[datetime] = None) -> None:
        """Load completed daily bars for all tracked instruments and seed engine streaks."""
        now = now_utc or datetime.now(timezone.utc)
        all_symbols = list(self.satellites) + [self.benchmark, self.safe_harbor]

        for sym in all_symbols:
            self.history[sym] = self._load_symbol_history(sym, now)
            self.logger.info("WARMUP symbol=%s bars=%d", sym, len(self.history[sym]))

        # Seed benchmark SMA and streak counters in engine
        bench_bars = self.history.get(self.benchmark, [])
        if len(bench_bars) >= self.dm_cfg.sma_period:
            closes = pd.Series([b.close for b in bench_bars])
            smas = compute_sma_200(closes, period=self.dm_cfg.sma_period)
            for close, sma in zip(closes, smas):
                if not pd.isna(sma):
                    self.engine.update_daily_streaks(close, sma)

        # Seed current holdings from position book
        self.engine.current_holdings = {
            pos.symbol for pos in self.book.all()
            if pos.qty > 0 and pos.symbol != self.safe_harbor
        }
        self.logger.info(
            "WARMUP_COMPLETE holdings=%s consecutive_above_sma=%d consecutive_below_sma=%d",
            self.engine.current_holdings, self.engine.consecutive_above_sma, self.engine.consecutive_below_sma
        )

    def _refresh_symbol_history(self, symbol: str, now_utc: datetime) -> None:
        end = today_midnight_et_utc(now_utc)
        hist = self.history.get(symbol, [])
        last_ts = hist[-1].ts if hist else None
        start = last_ts if last_ts is not None else end - timedelta(days=7)
        try:
            bars = self.data.get_bars(symbol, "equity", "1Day", start=start, end=end, use_cache=False)
            completed = self._completed_only(bars, now_utc)
            known = {b.ts for b in hist}
            for b in completed:
                if b.ts not in known:
                    hist.append(b)
            self.history[symbol] = hist[-self.history_bars:]
        except Exception as exc:
            self.logger.warning("REFRESH_FAILED symbol=%s error=%s", symbol, exc)

    def _account_equity(self) -> float:
        acct = self.alpaca.get_account()
        eq = float(acct.get("equity") or acct.get("portfolio_value") or 0)
        if eq <= 0:
            raise RuntimeError(f"Account returned non-positive equity: {eq}")
        return eq

    def _submit_limit_or_market(
        self,
        symbol: str,
        qty: int,
        side: str,
        role: str,
        limit_px: Optional[float] = None,
    ) -> dict:
        """Submit limit order at bid/ask midpoint or market order."""
        coid = make_client_order_id(self.strategy_name, _SETUP, symbol, role)
        if limit_px is not None and limit_px > 0:
            return self.alpaca.submit_order(
                symbol=symbol,
                qty=qty,
                side=side,
                order_type="limit",
                limit_price=limit_px,
                time_in_force="day",
                client_order_id=coid,
            )
        return self.alpaca.submit_order(
            symbol=symbol,
            qty=qty,
            side=side,
            order_type="market",
            time_in_force="day",
            client_order_id=coid,
        )

    def _execute_rebalance(
        self,
        target_weights: dict[str, float],
        latest_prices: dict[str, float],
    ) -> None:
        """Execute portfolio rebalancing: sell exits/trims first, then execute buys."""
        equity = self._account_equity()
        self.logger.info("REBALANCE_START equity=%.2f target_weights=%s", equity, target_weights)

        # Refresh book from MySQL
        try:
            fresh = self.mysql.load_open_positions()
            self.book.replace_from(fresh)
        except Exception as exc:
            self.logger.error("BOOK_REFRESH_FAILED error=%s", exc)

        current_positions = {p.symbol: p for p in self.book.all() if p.qty > 0}

        # Compute target shares for each symbol
        target_shares: dict[str, int] = {}
        for sym, weight in target_weights.items():
            if sym == self.safe_harbor:
                # Safe harbor / cash is held uninvested or in safe harbor ETF
                continue
            px = latest_prices.get(sym, 0.0)
            if px > 0 and weight > 0:
                allocated_dollars = equity * weight
                target_shares[sym] = int(allocated_dollars // px)
            else:
                target_shares[sym] = 0

        # Phase 1: SELLS (Exits and Trims)
        for sym, pos in list(current_positions.items()):
            if sym == self.safe_harbor:
                continue
            curr_qty = int(pos.qty)
            tgt_qty = target_shares.get(sym, 0)
            if tgt_qty < curr_qty:
                sell_qty = curr_qty - tgt_qty
                ref_px = latest_prices.get(sym, pos.entry_px)
                self.logger.info(
                    "REBALANCE_SELL symbol=%s curr_qty=%d tgt_qty=%d sell_qty=%d ref_px=%.4f",
                    sym, curr_qty, tgt_qty, sell_qty, ref_px
                )
                try:
                    order = self._submit_limit_or_market(
                        symbol=sym, qty=sell_qty, side="sell", role=Role.EXIT, limit_px=ref_px
                    )
                    fill_px = _fill_price(order, ref_px)
                    if tgt_qty == 0:
                        # Full exit
                        self.mysql.position_closed(
                            symbol=sym,
                            exit_px=fill_px,
                            exit_reason="etf_rotation_rebalance",
                            setup_name=_SETUP,
                            exit_client_order_id=(order or {}).get("client_order_id"),
                        )
                        self.book.close(sym, _SETUP)
                    else:
                        # Partial trim: update existing position qty
                        pos.qty = float(tgt_qty)
                        self.mysql.save_position(pos)
                except Exception as exc:
                    self.logger.error("SELL_FAILED symbol=%s error=%s", sym, exc)

        # Phase 2: BUYS (New entries and Additions)
        for sym, tgt_qty in target_shares.items():
            curr_pos = current_positions.get(sym)
            curr_qty = int(curr_pos.qty) if curr_pos else 0
            if tgt_qty > curr_qty:
                buy_qty = tgt_qty - curr_qty
                ref_px = latest_prices.get(sym, 0.0)
                if ref_px <= 0:
                    continue
                self.logger.info(
                    "REBALANCE_BUY symbol=%s curr_qty=%d tgt_qty=%d buy_qty=%d ref_px=%.4f",
                    sym, curr_qty, tgt_qty, buy_qty, ref_px
                )
                try:
                    order = self._submit_limit_or_market(
                        symbol=sym, qty=buy_qty, side="buy", role=Role.ENTRY, limit_px=ref_px
                    )
                    fill_px = _fill_price(order, ref_px)
                    if curr_pos is None:
                        new_pos = OpenPosition(
                            symbol=sym,
                            setup=_SETUP,
                            side="long",
                            qty=float(buy_qty),
                            entry_px=fill_px,
                            stop_px=None,
                            target_px=None,
                            opened_at=datetime.now(timezone.utc),
                            order_id=(order or {}).get("id", ""),
                            client_order_id=(order or {}).get("client_order_id"),
                            fill_confirmed=True,
                        )
                        self.book.add(new_pos)
                        self.mysql.position_opened(new_pos, "equity")
                        send_position_open_alert(
                            strategy_name=self.strategy_name,
                            symbol=sym,
                            side="long",
                            qty=buy_qty,
                            entry_px=fill_px,
                            setup_name=_SETUP,
                            asset_class="equity",
                        )
                    else:
                        curr_pos.qty = float(tgt_qty)
                        self.mysql.save_position(curr_pos)
                except Exception as exc:
                    self.logger.error("BUY_FAILED symbol=%s error=%s", sym, exc)

        self.logger.info("REBALANCE_COMPLETE")

    def run_cycle(self, now_utc: datetime) -> None:
        """Run daily evaluation and execute rebalance if triggered."""
        for sym in self.satellites + [self.benchmark, self.safe_harbor]:
            self._refresh_symbol_history(sym, now_utc)

        bench_bars = self.history.get(self.benchmark, [])
        if len(bench_bars) < self.dm_cfg.sma_period:
            self.logger.info("INSUFFICIENT_BENCHMARK_HISTORY bars=%d", len(bench_bars))
            return

        bench_closes = pd.Series([b.close for b in bench_bars])
        bench_smas = compute_sma_200(bench_closes, period=self.dm_cfg.sma_period)
        bench_close = float(bench_closes.iloc[-1])
        bench_sma = float(bench_smas.iloc[-1])

        # Get latest VIX value (or synthetic estimate from benchmark volatility if VIX bars unavailable)
        vix_bars = self.history.get(self.vix_symbol, [])
        if vix_bars:
            vix_val = float(vix_bars[-1].close)
        else:
            # Fallback realized volatility annualized * 100
            ret = bench_closes.pct_change().dropna().tail(20)
            vix_val = float(ret.std() * (252 ** 0.5) * 100) if len(ret) >= 5 else 18.0

        # Build prices DataFrame for satellite universe
        prices_dict = {}
        latest_prices = {}
        for sym in self.satellites:
            b_list = self.history.get(sym, [])
            if b_list:
                prices_dict[sym] = [b.close for b in b_list]
                latest_prices[sym] = float(b_list[-1].close)
            else:
                latest_prices[sym] = 0.0

        if not prices_dict:
            self.logger.warning("NO_SATELLITE_PRICES_AVAILABLE")
            return

        # Align series lengths
        min_len = min(len(v) for v in prices_dict.values())
        if min_len < self.dm_cfg.lookback_24w + 1:
            self.logger.info("INSUFFICIENT_MOMENTUM_HISTORY min_len=%d", min_len)
            return

        prices_df = pd.DataFrame({s: v[-min_len:] for s, v in prices_dict.items()})

        eval_date = now_utc.astimezone(_NY_TZ).date()
        step_res = self.engine.evaluate_step(
            eval_date=eval_date,
            prices_history=prices_df,
            benchmark_close=bench_close,
            benchmark_sma=bench_sma,
            vix_value=vix_val,
        )

        self.logger.info(
            "ETF_ROTATION_EVAL date=%s regime=%s rebalance=%s trigger=%s bench_close=%.2f "
            "bench_sma=%.2f vix=%.2f reason=%s",
            eval_date, step_res.regime.value, step_res.rebalance_triggered,
            step_res.trigger_type.value, bench_close, bench_sma, vix_val, step_res.reason
        )

        if step_res.rebalance_triggered:
            self._execute_rebalance(step_res.target_weights, latest_prices)

    def run(self) -> None:
        self.warmup()
        self.logger.info("ETF_ROTATION_BOT_STARTED satellites=%s", self.satellites)
        while not _shutdown:
            try:
                now = datetime.now(timezone.utc)
                next_open = next_market_open_utc(now, self.nyse)
                # Target execution time: 10:00 AM EST (15-30 min after market open)
                target = next_open + timedelta(minutes=self.execution_minute_offset, seconds=self.grace)
                wait = (target - now).total_seconds()
                if wait > 0:
                    self.logger.info("SLEEP until target execution time: %s", target.isoformat())
                    while not _shutdown and datetime.now(timezone.utc) < target:
                        time.sleep(min(60.0, (target - datetime.now(timezone.utc)).total_seconds()))
                    if _shutdown:
                        break
                self.run_cycle(datetime.now(timezone.utc))
            except Exception as exc:
                self.logger.error("ETF_ROTATION_LOOP_ERROR: %s", exc, exc_info=True)
                time.sleep(60)
        self.logger.info("ETF_ROTATION_SHUTDOWN")


def main() -> None:
    if "--config" not in sys.argv:
        raise SystemExit("main_etf_rotation.py requires --config <yaml>")
    idx = sys.argv.index("--config")
    if idx + 1 >= len(sys.argv):
        raise SystemExit("--config requires a path argument")
    cfg = load_config(sys.argv[idx + 1])
    system_name = cfg["system"]["name"]
    logger_ = setup_logging(log_file=cfg["logging"]["log_file"], logger_name=system_name)
    logger_.info("%s starting up; env=%s", system_name, cfg["system"]["trading_env"])

    _signal.signal(_signal.SIGTERM, _handle_shutdown)
    _signal.signal(_signal.SIGINT, _handle_shutdown)

    bot = EtfRotationBot(cfg, logger_)
    bot.run()


if __name__ == "__main__":
    main()
