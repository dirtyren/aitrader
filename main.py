"""VWAP Wave Protocol — autonomous intraday trading system.

Entry point: bar-close scheduler over a watchlist of equities + crypto on
Alpaca. Live trading is gated behind config (`system.trading_env`); paper by
default. The lock-file guard runs before any heavy import so an emergency
drawdown halt cannot be bypassed by a subsequent import error.
"""
from __future__ import annotations
import logging
import os
import signal as _signal
import sys
from datetime import datetime, timezone

import yaml

# ---------------------------------------------------------------------------
# Lock-file guard — must run before heavy imports.
# ---------------------------------------------------------------------------

_LOCK_FILE_PATH = os.environ.get("LOCK_FILE_PATH", "lock.file")
_TRADING_ENV = os.environ.get("TRADING_ENV", "production")

if _TRADING_ENV != "test" and os.path.exists(_LOCK_FILE_PATH):
    print("=" * 60)
    print("SYSTEM HALTED: Emergency lock file detected.")
    print(f"Lock file: {os.path.abspath(_LOCK_FILE_PATH)}")
    print("Resolve incident and remove lock.file before restarting.")
    print("=" * 60)
    sys.exit(1)

from broker.alpaca_client import AlpacaClient
from broker.alpaca_data import AlpacaData
from broker.order_executor import OrderExecutor
from core.asset_class import AssetClassConfig, session_start_for
from core.position_manager import PositionManager
from core.session import SessionContext
from risk.circuit_breakers import CircuitBreaker
from risk.filters import (
    ConcurrentPositionFilter, ConsecutiveLossFilter, FilterPipeline,
    NewsBlackout, NewsBlackoutFilter, RiskBudgetFilter,
    SessionWindowFilter, SetupCooldownFilter, SystemHaltedFilter,
    VolumeDeficitFilter,
)
from risk.manager import RiskManager
from risk.sizing import SizingConfig
from scheduler.bar_clock import next_boundary, sleep_until
from scheduler.loop import VWAPWaveEngine
from state.daily_ledger import DailyLedger
from state.dashboard_state import DashboardSnapshot, write_dashboard_state
from state.position_book import PositionBook
from strategies.setup_fade_extreme import FadeExtremeSetup
from strategies.setup_price_discovery import PriceDiscoverySetup
from strategies.setup_return_to_value import ReturnToValueSetup
from strategies.setup_vwap_bounce import VWAPBounceSetup
from ui.logging_setup import setup_logging


_shutdown = False


def _handle_shutdown(signum, frame):
    global _shutdown
    _shutdown = True


def load_config(path: str = "config/settings.yaml") -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def build_asset_class_configs(cfg: dict) -> dict[str, AssetClassConfig]:
    out: dict[str, AssetClassConfig] = {}
    for name, raw in cfg["asset_classes"].items():
        out[name] = AssetClassConfig(
            name=name,
            timezone=raw["timezone"],
            session_open_local=raw["session_open_local"],
            session_close_local=raw["session_close_local"],
            opening_blackout_min=cfg["filters"]["opening_blackout_min"],
            bar_timeframe=cfg["scheduler"]["bar_timeframe"],
            slippage_bps=raw.get("slippage_bps", 0.0),
            commission_per_share=raw.get("commission_per_share", 0.0),
            commission_bps=raw.get("commission_bps", 0.0),
        )
    return out


def build_setups(cfg: dict, symbol: str):
    s = cfg["setups"]
    setups = []
    if s["price_discovery"]["enabled"]:
        setups.append(PriceDiscoverySetup(
            symbol,
            atr_mult_stop=s["price_discovery"]["atr_mult_stop"],
            target_R=s["price_discovery"]["target_R"],
            arm_window_bars=s["price_discovery"]["arm_window_bars"],
        ))
    if s["fade_extreme"]["enabled"]:
        setups.append(FadeExtremeSetup(
            symbol,
            atr_mult_stop=s["fade_extreme"]["atr_mult_stop"],
            scale_offsets_atr=s["fade_extreme"]["scale_offsets_atr"],
            scale_weights=s["fade_extreme"]["scale_weights"],
        ))
    if s["return_to_value"]["enabled"]:
        setups.append(ReturnToValueSetup(
            symbol,
            atr_mult_stop=s["return_to_value"]["atr_mult_stop"],
            arm_window_bars=s["return_to_value"]["arm_window_bars"],
        ))
    if s["vwap_bounce"]["enabled"]:
        setups.append(VWAPBounceSetup(
            symbol,
            atr_mult_stop=s["vwap_bounce"]["atr_mult_stop"],
            target_R=s["vwap_bounce"]["target_R"],
            arm_window_bars=s["vwap_bounce"]["arm_window_bars"],
        ))
    return setups


def build_pipeline(cfg: dict, cb: CircuitBreaker) -> FilterPipeline:
    news_windows = [
        NewsBlackout(start=datetime.fromisoformat(w["start"]),
                     duration_min=w["duration_min"], label=w["label"])
        for w in cfg.get("news_blackouts") or []
    ]
    return FilterPipeline([
        SystemHaltedFilter(circuit_breaker=cb, lock_file_path=_LOCK_FILE_PATH),
        SessionWindowFilter(opening_blackout_min=cfg["filters"]["opening_blackout_min"]),
        NewsBlackoutFilter(windows=news_windows, pad_min=5),
        VolumeDeficitFilter(deficit_pct=cfg["filters"]["volume_deficit_pct"]),
        ConsecutiveLossFilter(limit=cfg["risk"]["consecutive_loss_limit"],
                              scope=cfg["risk"]["loss_filter_scope"]),
        ConcurrentPositionFilter(max_concurrent=cfg["risk"]["max_concurrent_positions"]),
        SetupCooldownFilter(cooldown_bars=cfg["setups"]["price_discovery"]["cooldown_bars"]),
        RiskBudgetFilter(daily_open_risk_cap_pct=cfg["risk"]["max_daily_risk_open"]),
    ])


def _collect_snapshot(symbols, contexts, book, ledger, cb,
                      recent_rejects: list[dict] | None = None) -> DashboardSnapshot:
    rows = []
    for sym, _ in symbols:
        ctx = contexts[sym]
        pos = book.get(sym)
        rows.append({
            "symbol": sym,
            "regime": ctx.regime,
            "vwap": None if ctx.bar_count == 0 else ctx.vwap,
            "upper": None if ctx.bar_count == 0 else ctx.upper_band,
            "lower": None if ctx.bar_count == 0 else ctx.lower_band,
            "open_position": None if pos is None else {
                "side": pos.side, "qty": pos.qty,
                "entry": pos.entry_px, "stop": pos.stop_px, "target": pos.target_px,
            },
        })
    return DashboardSnapshot(
        timestamp=datetime.now(timezone.utc),
        equity=ledger.equity,
        day_pnl=ledger.day_pnl,
        circuit_level=cb.level,
        symbols=rows,
        recent_filter_rejects=(recent_rejects or [])[-20:],
    )


def main():
    cfg = load_config()
    logger = setup_logging(log_file=cfg["logging"]["log_file"])
    logger.info("vwap_wave starting up; env=%s", cfg["system"]["trading_env"])

    _signal.signal(_signal.SIGTERM, _handle_shutdown)
    _signal.signal(_signal.SIGINT, _handle_shutdown)

    ac_configs = build_asset_class_configs(cfg)

    symbols: list[tuple[str, str]] = []
    for ac_name, raw in cfg["asset_classes"].items():
        for sym in raw["symbols"]:
            symbols.append((sym, ac_name))

    contexts = {sym: SessionContext(symbol=sym, asset_class=ac_configs[ac])
                for sym, ac in symbols}
    setups = {sym: build_setups(cfg, sym) for sym, _ in symbols}

    alpaca = AlpacaClient()
    data = AlpacaData(alpaca, cache_dir=cfg["backtest"]["cache_dir"])
    book = PositionBook()

    account = alpaca.get_account()
    initial_equity = float(account.get("equity") or account.get("portfolio_value") or 0)
    if initial_equity <= 0:
        logger.error("Account returned non-positive equity; aborting")
        sys.exit(1)
    ledger = DailyLedger(initial_equity=initial_equity)

    cb_cfg = cfg["risk"]["circuit_breaker"]
    cb = CircuitBreaker(
        peak_equity=initial_equity,
        daily_loss_limit_1=cb_cfg["daily_loss_limit_1"],
        daily_loss_limit_2=cb_cfg["daily_loss_limit_2"],
        drawdown_limit=cb_cfg["drawdown_limit"],
    )

    pipeline = build_pipeline(cfg, cb)

    sizing_eq = SizingConfig(
        max_risk_per_trade=cfg["risk"]["max_risk_per_trade"],
        max_notional_per_trade_pct=cfg["risk"]["max_notional_per_trade_pct"],
        allow_fractional=False,
    )
    sizing_cr = SizingConfig(
        max_risk_per_trade=cfg["risk"]["max_risk_per_trade"],
        max_notional_per_trade_pct=cfg["risk"]["max_notional_per_trade_pct"],
        allow_fractional=True,
    )

    rm = RiskManager(
        circuit_breaker=cb, pipeline=pipeline,
        sizing_equity=sizing_eq, sizing_crypto=sizing_cr,
        ledger=ledger, book=book,
    )
    executor = OrderExecutor(alpaca, book, logger=logger)
    pm = PositionManager(
        book,
        max_hold_bars=cfg["position_management"]["max_hold_bars"],
        breakeven_at_R=cfg["position_management"]["breakeven_at_R"],
    )
    engine = VWAPWaveEngine(
        symbols=symbols, contexts=contexts, setups=setups,
        risk_manager=rm, executor=executor, book=book, ledger=ledger,
        position_manager=pm,
    )

    timeframe = cfg["scheduler"]["bar_timeframe"]
    grace = cfg["scheduler"]["wake_grace_seconds"]
    logger.info("vwap_wave loop starting; symbols=%d", len(symbols))

    while not _shutdown:
        now = datetime.now(timezone.utc)
        target = next_boundary(now, timeframe, grace_seconds=grace)
        sleep_until(target)
        if _shutdown:
            break

        try:
            cycle_now = datetime.now(timezone.utc)
            fresh_bars: dict[str, list] = {}
            for sym, ac_name in symbols:
                ctx = contexts[sym]
                ac = ac_configs[ac_name]
                start = session_start_for(cycle_now, ac)
                bars = data.get_bars(sym, ac_name, timeframe, start=start, end=cycle_now,
                                     use_cache=False)
                last_known_ts = ctx.bars[-1].ts if ctx.bars else None
                new_bars = [b for b in bars if last_known_ts is None or b.ts > last_known_ts]
                if new_bars:
                    fresh_bars[sym] = new_bars
            engine.tick(now=cycle_now, fresh_bars=fresh_bars)

            account = alpaca.get_account()
            equity = float(account.get("equity") or account.get("portfolio_value") or ledger.equity)
            rm.update_equity(equity)

            daily_pnl_pct = ledger.day_pnl / initial_equity if initial_equity > 0 else 0.0
            cb.check(equity, daily_pnl_pct)

            snap = _collect_snapshot(symbols, contexts, book, ledger, cb)
            write_dashboard_state("runtime/trading_state.json", snap)
        except Exception as exc:
            logger.error("CYCLE_ERROR: %s", exc, exc_info=True)

    logger.info("Shutdown requested. Closing.")


if __name__ == "__main__":
    main()
