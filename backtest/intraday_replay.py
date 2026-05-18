from __future__ import annotations
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import pandas as pd

from backtest.fill_engine import PendingOrder, SimulatedFillEngine
from core.asset_class import AssetClassConfig
from core.bar import Bar
from core.position_manager import PositionManager
from core.session import SessionContext
from risk.circuit_breakers import CircuitBreaker
from risk.filters import (
    ConcurrentPositionFilter, ConsecutiveLossFilter, FilterPipeline,
    RiskBudgetFilter, SessionWindowFilter, SetupCooldownFilter,
    SystemHaltedFilter, VolumeDeficitFilter,
)
from risk.manager import RiskManager
from risk.sizing import SizingConfig
from scheduler.loop import record_exits_to_ledger
from state.daily_ledger import DailyLedger, TradeRecord
from state.position_book import PositionBook
from strategies.base_setup import BaseSetup, SetupSignal
from strategies.setup_fade_extreme import FadeExtremeSetup
from strategies.setup_price_discovery import PriceDiscoverySetup
from strategies.setup_return_to_value import ReturnToValueSetup
from strategies.setup_vwap_bounce import VWAPBounceSetup

logger = logging.getLogger(__name__)


_LIMIT_SETUPS = ("price_discovery", "return_to_value", "vwap_bounce")


@dataclass
class BacktestResult:
    trades: pd.DataFrame
    equity_curve: pd.Series
    per_setup: dict[str, dict]
    per_symbol: dict[str, dict]
    metrics: dict
    filter_audit: dict[str, int]


def _build_setups(cfg: dict, symbol: str) -> list[BaseSetup]:
    s = cfg["setups"]
    out: list[BaseSetup] = []
    if s["price_discovery"]["enabled"]:
        out.append(PriceDiscoverySetup(
            symbol,
            atr_mult_stop=s["price_discovery"]["atr_mult_stop"],
            target_R=s["price_discovery"]["target_R"],
            arm_window_bars=s["price_discovery"]["arm_window_bars"],
        ))
    if s["fade_extreme"]["enabled"]:
        out.append(FadeExtremeSetup(
            symbol,
            atr_mult_stop=s["fade_extreme"]["atr_mult_stop"],
            scale_offsets_atr=s["fade_extreme"]["scale_offsets_atr"],
            scale_weights=s["fade_extreme"]["scale_weights"],
        ))
    if s["return_to_value"]["enabled"]:
        out.append(ReturnToValueSetup(
            symbol,
            atr_mult_stop=s["return_to_value"]["atr_mult_stop"],
            arm_window_bars=s["return_to_value"]["arm_window_bars"],
        ))
    if s["vwap_bounce"]["enabled"]:
        out.append(VWAPBounceSetup(
            symbol,
            atr_mult_stop=s["vwap_bounce"]["atr_mult_stop"],
            target_R=s["vwap_bounce"]["target_R"],
            arm_window_bars=s["vwap_bounce"]["arm_window_bars"],
        ))
    return out


def _max_consecutive_losses(trades: pd.DataFrame) -> int:
    if trades.empty:
        return 0
    streak = best = 0
    for v in trades["pnl_usd"]:
        if v < 0:
            streak += 1
            best = max(best, streak)
        else:
            streak = 0
    return best


@dataclass
class IntradayReplay:
    """Drive the live risk + position-management stack over historical bars.

    Per-bar order:
      1. Phase A — manage open positions: ingest into ctx, run PositionManager,
         record exits in ledger via the same helper the live engine uses.
      2. fill.process_bar — resolve pending orders submitted on earlier bars
         against THIS bar's OHLC (fresh fills enter the book here).
      3. Phase B — setups detect, RiskManager.evaluate; approved signals are
         enqueued as PendingOrder for the NEXT bar.
    """

    symbols: list[tuple[str, str]]
    asset_class_configs: dict[str, AssetClassConfig]
    bars: dict[str, list[Bar]]
    initial_equity: float
    config: dict

    def run(self) -> BacktestResult:
        contexts = {sym: SessionContext(symbol=sym, asset_class=self.asset_class_configs[ac])
                    for sym, ac in self.symbols}
        setups: dict[str, list[BaseSetup]] = {sym: _build_setups(self.config, sym)
                                              for sym, _ in self.symbols}
        book = PositionBook()
        ledger = DailyLedger(initial_equity=self.initial_equity)
        cb_cfg = self.config["risk"]["circuit_breaker"]
        cb = CircuitBreaker(
            peak_equity=self.initial_equity,
            daily_loss_limit_1=cb_cfg["daily_loss_limit_1"],
            daily_loss_limit_2=cb_cfg["daily_loss_limit_2"],
            drawdown_limit=cb_cfg["drawdown_limit"],
        )
        # Filters: SessionWindowFilter is permissive when opening_blackout_min == 0
        # (which is the backtest default); NewsBlackoutFilter is omitted entirely.
        pipeline = FilterPipeline([
            SystemHaltedFilter(circuit_breaker=cb, lock_file_path="/__nonexistent__"),
            SessionWindowFilter(opening_blackout_min=self.config["filters"]["opening_blackout_min"]),
            VolumeDeficitFilter(deficit_pct=self.config["filters"]["volume_deficit_pct"]),
            ConsecutiveLossFilter(limit=self.config["risk"]["consecutive_loss_limit"],
                                  scope=self.config["risk"]["loss_filter_scope"]),
            ConcurrentPositionFilter(max_concurrent=self.config["risk"]["max_concurrent_positions"]),
            SetupCooldownFilter(cooldown_bars=self.config["setups"]["price_discovery"]["cooldown_bars"]),
            RiskBudgetFilter(daily_open_risk_cap_pct=self.config["risk"]["max_daily_risk_open"]),
        ])
        sizing_eq = SizingConfig(
            max_risk_per_trade=self.config["risk"]["max_risk_per_trade"],
            max_notional_per_trade_pct=self.config["risk"]["max_notional_per_trade_pct"],
            allow_fractional=False,
        )
        sizing_cr = SizingConfig(
            max_risk_per_trade=self.config["risk"]["max_risk_per_trade"],
            max_notional_per_trade_pct=self.config["risk"]["max_notional_per_trade_pct"],
            allow_fractional=True,
        )
        rm = RiskManager(
            circuit_breaker=cb, pipeline=pipeline,
            sizing_equity=sizing_eq, sizing_crypto=sizing_cr,
            ledger=ledger, book=book,
        )
        slippage = {ac.name: ac.slippage_bps for ac in self.asset_class_configs.values()}
        fill = SimulatedFillEngine(slippage_bps_by_class=slippage)
        pm = PositionManager(
            book,
            max_hold_bars=self.config["position_management"]["max_hold_bars"],
            breakeven_at_R=self.config["position_management"]["breakeven_at_R"],
        )

        # Build a chronological timeline across all symbols. Stable secondary
        # key on symbol gives deterministic ordering when timestamps tie.
        timeline: list[tuple[datetime, str, str, Bar]] = []
        for sym, ac in self.symbols:
            for b in self.bars.get(sym, []):
                timeline.append((b.ts, sym, ac, b))
        timeline.sort(key=lambda x: (x[0], x[1]))

        first_ts = timeline[0][0] if timeline else datetime.now(timezone.utc)
        equity_points: list[tuple[datetime, float]] = [(first_ts, ledger.equity)]
        trades_log: list[TradeRecord] = []
        filter_audit: dict[str, int] = {}

        for ts, sym, ac, bar in timeline:
            ctx = contexts[sym]
            ctx.ingest(bar)

            # Phase A — manage positions opened on PRIOR bars
            pos_before = book.get(sym)
            actions = pm.on_bar(sym, bar)
            recorded = record_exits_to_ledger(ledger, sym, actions, bar, pos_before)
            trades_log.extend(recorded)

            # Resolve pending fills against this bar's OHLC. Newly-filled
            # positions enter the book here and won't be managed until the
            # next bar — avoids using future intra-bar data.
            fill.process_bar(sym, bar, book)

            # Phase B — detect new entries, enqueue for next bar
            for setup in setups[sym]:
                signal = setup.check(ctx)
                if signal is None:
                    continue
                decision = rm.evaluate(signal, ctx, ac)
                if not decision.approved:
                    key = decision.reason.split(":")[0] if decision.reason else "unknown"
                    filter_audit[key] = filter_audit.get(key, 0) + 1
                    continue
                fill.submit(PendingOrder(
                    symbol=sym,
                    side="buy" if signal.side == "long" else "sell",
                    qty=decision.qty,
                    order_type="limit" if signal.setup in _LIMIT_SETUPS else "market",
                    limit_price=signal.entry,
                    stop_price=signal.stop,
                    target_price=signal.target,
                    asset_class=ac, setup=signal.setup, ts=signal.ts,
                ))

            equity_points.append((bar.ts, ledger.equity))

        return _build_result(trades_log, equity_points, ledger,
                             self.initial_equity, filter_audit)


def _build_result(trades_log: list[TradeRecord],
                  equity_points: list[tuple[datetime, float]],
                  ledger: DailyLedger, initial_equity: float,
                  filter_audit: dict[str, int]) -> BacktestResult:
    trades_df = pd.DataFrame([t.__dict__ for t in trades_log])
    eq_series = pd.Series([e for _, e in equity_points],
                          index=[t for t, _ in equity_points])

    per_setup: dict[str, dict] = {}
    per_symbol: dict[str, dict] = {}
    if not trades_df.empty:
        for s, g in trades_df.groupby("setup"):
            wins = g.loc[g["pnl_usd"] > 0, "pnl_usd"].sum()
            losses = abs(g.loc[g["pnl_usd"] < 0, "pnl_usd"].sum())
            per_setup[s] = {
                "trades": int(len(g)),
                "win_rate": float((g["pnl_usd"] > 0).mean()),
                "expectancy_R": float(g["R_realized"].mean()),
                "profit_factor": float(wins / losses) if losses > 0 else float("inf"),
            }
        for s, g in trades_df.groupby("symbol"):
            per_symbol[s] = {
                "trades": int(len(g)),
                "total_pnl": float(g["pnl_usd"].sum()),
            }

    metrics = {
        "trades": int(len(trades_df)),
        "final_equity": float(ledger.equity),
        "total_return": float((ledger.equity - initial_equity) / initial_equity)
                        if initial_equity else 0.0,
        "max_consecutive_losses": int(_max_consecutive_losses(trades_df)),
        "win_rate": float((trades_df["pnl_usd"] > 0).mean()) if not trades_df.empty else 0.0,
        "avg_R": float(trades_df["R_realized"].mean()) if not trades_df.empty else 0.0,
    }

    return BacktestResult(
        trades=trades_df, equity_curve=eq_series,
        per_setup=per_setup, per_symbol=per_symbol,
        metrics=metrics, filter_audit=filter_audit,
    )
