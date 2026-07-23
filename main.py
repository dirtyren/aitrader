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
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml

from broker.alpaca_client import AlpacaClient
from broker.alpaca_data import AlpacaData
from broker.order_executor import OrderExecutor
from core.asset_class import AssetClassConfig, session_start_for
from core.position_manager import PositionManager
from core.session import SessionContext
from risk.filters import (
    BrokerPositionFilter, ConcurrentPositionFilter,
    ConsecutiveLossFilter, FilterPipeline,
    ManualCloseCooldownFilter,
    NewsBlackout, NewsBlackoutFilter, RiskBudgetFilter,
    SessionWindowFilter, SetupCooldownFilter,
    VolumeDeficitFilter,
)
from risk.manager import RiskManager
from risk.sizing import SizingConfig
from scheduler.bar_clock import next_boundary, sleep_until
from scheduler.loop import VWAPWaveEngine
from state.daily_ledger import DailyLedger
from state.dashboard_state import DashboardSnapshot, write_dashboard_state
from state.mysql_store import MySQLStore
from strategies.setup_cmf import CMFSetup
from strategies.setup_fade_extreme import FadeExtremeSetup
from strategies.setup_price_discovery import PriceDiscoverySetup
from strategies.setup_return_to_value import ReturnToValueSetup
from strategies.setup_vwap_bounce import VWAPBounceSetup
from ui.logging_setup import setup_logging


_shutdown = False


def _handle_shutdown(signum, frame):
    global _shutdown
    _shutdown = True


def load_config(path: str) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def apply_overrides(cfg: dict, overrides_path: str | None,
                    *, enabled: bool = True) -> dict:
    """Layer per-symbol WFO overrides on top of the loaded settings."""
    if not enabled or not overrides_path:
        return cfg
    from pathlib import Path
    if not Path(overrides_path).exists():
        return cfg
    payload = yaml.safe_load(Path(overrides_path).read_text()) or {}
    symbols = payload.get("symbols") or {}
    if symbols:
        cfg["_per_symbol_overrides"] = symbols
    return cfg


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
    overrides = cfg.get("_per_symbol_overrides") or {}
    if symbol in overrides:
        return _build_setups_from_override(symbol, overrides[symbol])
    s = cfg.get("setups", {})
    setups = []
    if "price_discovery" in s and s["price_discovery"].get("enabled", False):
        setups.append(PriceDiscoverySetup(
            symbol,
            atr_mult_stop=s["price_discovery"]["atr_mult_stop"],
            target_R=s["price_discovery"]["target_R"],
            arm_window_bars=s["price_discovery"]["arm_window_bars"],
        ))
    if "fade_extreme" in s and s["fade_extreme"].get("enabled", False):
        setups.append(FadeExtremeSetup(
            symbol,
            atr_mult_stop=s["fade_extreme"]["atr_mult_stop"],
            scale_offsets_atr=s["fade_extreme"].get("scale_offsets_atr"),
            scale_weights=s["fade_extreme"].get("scale_weights"),
        ))
    if "return_to_value" in s and s["return_to_value"].get("enabled", False):
        setups.append(ReturnToValueSetup(
            symbol,
            atr_mult_stop=s["return_to_value"]["atr_mult_stop"],
            arm_window_bars=s["return_to_value"]["arm_window_bars"],
        ))
    if "vwap_bounce" in s and s["vwap_bounce"].get("enabled", False):
        setups.append(VWAPBounceSetup(
            symbol,
            atr_mult_stop=s["vwap_bounce"]["atr_mult_stop"],
            target_R=s["vwap_bounce"]["target_R"],
            arm_window_bars=s["vwap_bounce"]["arm_window_bars"],
        ))
    if "rsi_reversion" in s and s["rsi_reversion"].get("enabled", False):
        from strategies.setup_rsi import RSISetup
        setups.append(RSISetup(
            symbol,
            threshold=s["rsi_reversion"]["threshold"],
            direction=s["rsi_reversion"]["direction"],
            stop_loss_pct=s["rsi_reversion"]["stop_loss_pct"],
            position_size_r=s["rsi_reversion"]["position_size_r"],
            period=s["rsi_reversion"]["period"],
        ))
    if "initial_balance" in s and s["initial_balance"].get("enabled", False):
        from strategies.setup_initial_balance import InitialBalanceSetup
        setups.append(InitialBalanceSetup(
            symbol,
            ib_bars=s["initial_balance"].get("ib_bars", 6),
            atr_mult_stop=s["initial_balance"]["atr_mult_stop"],
            target_R=s["initial_balance"]["target_R"],
        ))
    if "vwap_dev_bands" in s and s["vwap_dev_bands"].get("enabled", False):
        from strategies.setup_vwap_dev_bands import VWAPDevBandsSetup
        setups.append(VWAPDevBandsSetup(
            symbol,
            sigma=s["vwap_dev_bands"].get("sigma", 2.5),
            atr_mult_stop=s["vwap_dev_bands"]["atr_mult_stop"],
            target_R=s["vwap_dev_bands"]["target_R"],
        ))
    if "orb_vwap" in s and s["orb_vwap"].get("enabled", False):
        from strategies.setup_orb_vwap import ORBVWAPSetup
        setups.append(ORBVWAPSetup(
            symbol,
            orb_bars=s["orb_vwap"].get("orb_bars", 3),
            atr_mult_stop=s["orb_vwap"]["atr_mult_stop"],
            target_R=s["orb_vwap"]["target_R"],
        ))
    if "cmf" in s and s["cmf"].get("enabled", False):
        setups.append(CMFSetup(
            symbol,
            period=s["cmf"].get("period", 20),
            threshold=s["cmf"].get("threshold", 0.10),
            atr_mult_stop=s["cmf"]["atr_mult_stop"],
            target_R=s["cmf"]["target_R"],
        ))
    return setups


_OVERRIDE_FACTORIES = {
    "price_discovery": lambda symbol, p: PriceDiscoverySetup(
        symbol,
        atr_mult_stop=p["atr_mult_stop"],
        target_R=p["target_R"],
        arm_window_bars=p["arm_window_bars"],
    ),
    "fade_extreme": lambda symbol, p: FadeExtremeSetup(
        symbol,
        atr_mult_stop=p["atr_mult_stop"],
        scale_offsets_atr=p.get("scale_offsets_atr", [0.0, 0.25, 0.5]),
        scale_weights=p.get("scale_weights", [0.4, 0.35, 0.25]),
    ),
    "return_to_value": lambda symbol, p: ReturnToValueSetup(
        symbol,
        atr_mult_stop=p["atr_mult_stop"],
        arm_window_bars=p["arm_window_bars"],
    ),
    "vwap_bounce": lambda symbol, p: VWAPBounceSetup(
        symbol,
        atr_mult_stop=p["atr_mult_stop"],
        target_R=p["target_R"],
        arm_window_bars=p["arm_window_bars"],
    ),
    "cmf": lambda symbol, p: CMFSetup(
        symbol,
        period=p.get("period", 20),
        threshold=p.get("threshold", 0.10),
        atr_mult_stop=p["atr_mult_stop"],
        target_R=p["target_R"],
    ),
}


def _build_setups_from_override(symbol: str, override: dict):
    setup_name = override["setup"]
    factory = _OVERRIDE_FACTORIES.get(setup_name)
    if factory is None:
        raise ValueError(f"Unknown setup in override for {symbol}: {setup_name!r}")
    return [factory(symbol, override["setup_params"])]


def position_manager_for(
    symbol: str, cfg: dict, book,
    *,
    alpaca=None,
    mysql=None,
    strategy_id: int | None = None,
) -> PositionManager:
    overrides = cfg.get("_per_symbol_overrides") or {}
    pm_cfg = (overrides.get(symbol, {}).get("position_management")
              if symbol in overrides else cfg["position_management"])

    def _order_status_for(pos):
        # Lookup the broker order's status. Errors propagate so the
        # PositionManager can log and treat as 'unknown' (no exit).
        if alpaca is None or not pos.order_id:
            return None
        order = alpaca.get_order(pos.order_id)
        if not isinstance(order, dict):
            return None
        return order.get("status")

    def _on_fill_confirmed(pos):
        # Persist the flip so a restart doesn't re-poll. mysql may be None
        # in unit tests / dry-run modes.
        if mysql is None or strategy_id is None:
            return
        mysql.mark_fill_confirmed(strategy_id, pos.symbol, pos.setup)

    return PositionManager(
        book,
        max_hold_bars=pm_cfg["max_hold_bars"],
        breakeven_at_R=pm_cfg["breakeven_at_R"],
        order_status_for=_order_status_for,
        on_fill_confirmed=_on_fill_confirmed,
    )


def timeframe_for(symbol: str, cfg: dict) -> str:
    overrides = cfg.get("_per_symbol_overrides") or {}
    if symbol in overrides and "timeframe" in overrides[symbol]:
        return overrides[symbol]["timeframe"]
    return cfg["scheduler"]["bar_timeframe"]


def finest_timeframe(symbols: list[tuple[str, str]], cfg: dict) -> str:
    from scheduler.bar_clock import parse_timeframe_minutes
    candidates = {timeframe_for(sym, cfg) for sym, _ in symbols}
    return min(candidates, key=parse_timeframe_minutes)


class _PerSymbolPositionManager:
    """Routes on_bar(symbol, bar) to a per-symbol PositionManager."""

    def __init__(self, per_symbol: dict, fallback):
        self._per_symbol = per_symbol
        self._fallback = fallback

    def on_bar(self, symbol, bar):
        pm = self._per_symbol.get(symbol, self._fallback)
        return pm.on_bar(symbol, bar)


def build_pipeline(cfg: dict, ac_configs: dict | None = None,
                   alpaca=None,
                   mysql=None, strategy_id: int | None = None) -> FilterPipeline:
    news_windows = [
        NewsBlackout(start=datetime.fromisoformat(w["start"]),
                     duration_min=w["duration_min"], label=w["label"])
        for w in cfg.get("news_blackouts") or []
    ]
    filters = [
        SessionWindowFilter(
            opening_blackout_min=cfg["filters"]["opening_blackout_min"],
            asset_class_configs=ac_configs,
        ),
        NewsBlackoutFilter(windows=news_windows, pad_min=5),
        VolumeDeficitFilter(deficit_pct=cfg["filters"]["volume_deficit_pct"]),
        ConsecutiveLossFilter(limit=cfg["risk"]["consecutive_loss_limit"],
                              scope=cfg["risk"]["loss_filter_scope"]),
        ConcurrentPositionFilter(max_concurrent=cfg["risk"]["max_concurrent_positions"]),
    ]
    if alpaca is not None:
        # Cross-strategy / reconciler-drift safety net: refuse new entries
        # whenever the broker already holds inventory on the symbol, even if
        # this strategy's book is empty. Cache TTL keeps it cheap; defaults
        # to 30s, override via BROKER_POSITION_FILTER_TTL_S env.
        filters.append(BrokerPositionFilter(
            broker=alpaca,
            cache_ttl_s=float(os.environ.get("BROKER_POSITION_FILTER_TTL_S", "30")),
        ))
    if mysql is not None and strategy_id is not None:
        # Honour reconciler-detected manual closes: when the operator (or an
        # external risk system) flattens a position at the broker without
        # going through aitrader, the reconciler inserts a cooldown row and
        # this filter blocks re-entry for the configured window.
        filters.append(ManualCloseCooldownFilter(
            store=mysql, strategy_id=strategy_id,
            cache_ttl_s=float(os.environ.get("MANUAL_CLOSE_CACHE_TTL_S", "30")),
        ))
    filters.extend([
        SetupCooldownFilter(cooldown_bars=cfg.get("setups", {}).get("price_discovery", {}).get("cooldown_bars", 12)),
        RiskBudgetFilter(daily_open_risk_cap_pct=cfg["risk"]["max_daily_risk_open"]),
    ])
    return FilterPipeline(filters)


def _collect_snapshot(symbols, contexts, book, ledger,
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
            "last_price": None if ctx.bar_count == 0 else ctx.bars[-1].close,
            "open_position": None if pos is None else {
                "side": pos.side, "qty": pos.qty,
                "entry": pos.entry_px, "stop": pos.stop_px, "target": pos.target_px,
            },
        })
    return DashboardSnapshot(
        timestamp=datetime.now(timezone.utc),
        equity=ledger.equity,
        day_pnl=ledger.day_pnl,
        symbols=rows,
        recent_filter_rejects=(recent_rejects or [])[-20:],
    )


def main():
    import sys
    if "--config" not in sys.argv:
        raise SystemExit(
            "main.py requires --config <path-to-strategy-yaml>; "
            "no implicit default after the per-asset-class split."
        )
    idx = sys.argv.index("--config")
    if idx + 1 >= len(sys.argv):
        raise SystemExit("--config requires a path argument")
    config_path = sys.argv[idx + 1]

    cfg = load_config(config_path)
    overrides_cfg = cfg.get("overrides") or {}
    cfg = apply_overrides(cfg, overrides_cfg.get("path"),
                          enabled=overrides_cfg.get("enabled", True))
    system_name = cfg["system"]["name"]
    logger = setup_logging(log_file=cfg["logging"]["log_file"], logger_name=system_name)
    logger.info("%s starting up; env=%s", system_name, cfg["system"]["trading_env"])

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

    asset_class = next(iter(cfg["asset_classes"].keys()))
    alpaca = AlpacaClient(asset_class=asset_class)
    data = AlpacaData(alpaca, cache_dir=cfg["backtest"]["cache_dir"])

    # ── MySQL store (mandatory source of truth for positions/trades) ──
    mysql = MySQLStore(strategy_name=system_name, logger=logger)
    _MYSQL_RETRIES = 5
    _MYSQL_RETRY_DELAY_S = 30
    for attempt in range(1, _MYSQL_RETRIES + 1):
        try:
            mysql.ensure_schema()
            mysql.upsert_strategy()
            logger.info("MYSQL_CONNECTED strategy=%s attempt=%d", system_name, attempt)
            break
        except Exception as exc:
            if attempt == _MYSQL_RETRIES:
                logger.error(
                    "MYSQL_UNREACHABLE after %d attempts; aborting startup: %s",
                    attempt, exc, exc_info=True,
                )
                sys.exit(1)
            logger.warning(
                "MYSQL_CONNECT_RETRY attempt=%d/%d: %s",
                attempt, _MYSQL_RETRIES, exc,
            )
            time.sleep(_MYSQL_RETRY_DELAY_S)

    # ── One-time legacy JSON migration (idempotent) ──
    legacy_json = Path(
        os.environ.get("POSITION_BOOK_PATH",
                       f"runtime/position_book_{system_name}.json")
    )
    asset_class_for_symbol = {
        sym: ac_name
        for ac_name, raw in cfg["asset_classes"].items()
        for sym in raw["symbols"]
    }
    try:
        migrated = mysql.migrate_legacy_json(
            legacy_json,
            asset_class_for=lambda s: asset_class_for_symbol.get(s),
        )
        if migrated > 0:
            logger.info("MYSQL_LEGACY_MIGRATED rows=%d", migrated)
    except Exception as exc:
        logger.error("MYSQL_LEGACY_MIGRATION_FAILED: %s", exc, exc_info=True)

    book = mysql.load_open_positions()
    logger.info(
        "POSITION_BOOK_LOADED_FROM_MYSQL strategy=%s open_positions=%d",
        system_name, book.count(),
    )

    account = alpaca.get_account()
    initial_equity = float(account.get("equity") or account.get("portfolio_value") or 0)
    if initial_equity <= 0:
        logger.error("Account returned non-positive equity; aborting")
        sys.exit(1)
    _acct_num = str(account.get("account_number") or "")
    _acct_masked = f"{_acct_num[:4]}***" if len(_acct_num) >= 4 else "***"
    logger.info(
        "ALPACA_ACCOUNT_BOUND asset_class=%s account_number=%s equity=%.2f base_url=%s",
        asset_class, _acct_masked, initial_equity, alpaca.base_url,
    )
    ledger = DailyLedger(initial_equity=initial_equity)

    pipeline = build_pipeline(
        cfg, ac_configs=ac_configs, alpaca=alpaca, mysql=mysql,
        strategy_id=mysql.strategy_id if mysql is not None else None,
    )

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
        pipeline=pipeline,
        sizing_equity=sizing_eq, sizing_crypto=sizing_cr,
        ledger=ledger, book=book,
    )
    executor = OrderExecutor(alpaca, book, strategy_name=system_name,
                             logger=logger, mysql_store=mysql)

    # When overrides exist, each symbol may want its own PositionManager. The
    # engine still receives a single PM; we wire a dispatcher that routes
    # on_bar(symbol, bar) to the right per-symbol PM.
    overrides = cfg.get("_per_symbol_overrides") or {}
    pm_kwargs = dict(
        alpaca=alpaca,
        mysql=mysql,
        strategy_id=mysql.strategy_id if mysql is not None else None,
    )
    if overrides:
        per_symbol_pms = {
            sym: position_manager_for(sym, cfg, book, **pm_kwargs)
            for sym, _ in symbols
        }
        pm = _PerSymbolPositionManager(
            per_symbol_pms,
            fallback=position_manager_for(
                "__default__", cfg, book, **pm_kwargs,
            ),
        )
    else:
        pm = position_manager_for("__default__", cfg, book, **pm_kwargs)

    engine = VWAPWaveEngine(
        symbols=symbols, contexts=contexts, setups=setups,
        risk_manager=rm, executor=executor, book=book, ledger=ledger,
        position_manager=pm, mysql_store=mysql,
    )

    timeframe = finest_timeframe(symbols, cfg)
    grace = cfg["scheduler"]["wake_grace_seconds"]
    logger.info("%s loop starting; symbols=%d finest_tf=%s",
                system_name, len(symbols), timeframe)

    while not _shutdown:
        now = datetime.now(timezone.utc)
        target = next_boundary(now, timeframe, grace_seconds=grace)
        sleep_until(target)
        if _shutdown:
            break

        try:
            cycle_now = datetime.now(timezone.utc)
            executor.reset_cycle()

            # MySQL is the source of truth — rebuild the book from MySQL each
            # cycle so deletions/closures by other processes are reflected.
            try:
                fresh_book = mysql.load_open_positions()
                book.replace_from(fresh_book)
            except Exception as exc:
                logger.error("MYSQL_REBUILD_FAILED: %s", exc, exc_info=True)

            # Operator kill-switch. When `enabled=False`, sweep any open
            # positions and skip the rest of the cycle. This is also the
            # self-healing path for partial dashboard sweeps: the dashboard
            # leaves state='disabling' on failures and the trader retries
            # here every cycle until book is empty, then flips to 'disabled'.
            if not mysql.is_strategy_enabled():
                if book.count() > 0:
                    from state.strategy_close_all import close_all_open_positions
                    result = close_all_open_positions(
                        alpaca=alpaca, mysql=mysql,
                        strategy_name=system_name,
                        reason="trader_disable_sweep",
                    )
                    logger.warning(
                        "STRATEGY_DISABLED_SWEEP closed=%d failed=%d",
                        len(result.closed), len(result.failed),
                    )
                    try:
                        fresh_book = mysql.load_open_positions()
                        book.replace_from(fresh_book)
                    except Exception as exc:
                        logger.error("MYSQL_REBUILD_FAILED: %s", exc, exc_info=True)
                if book.count() == 0:
                    cur_state = mysql.get_strategy_state(mysql.strategy_id)
                    if cur_state != "disabled":
                        mysql.set_strategy_state(
                            strategy_id=mysql.strategy_id,
                            enabled=False, state="disabled",
                            reason="trader_disable_sweep_complete",
                        )
                        logger.info("STRATEGY_DISABLED state=disabled")
                continue

            fresh_bars: dict[str, list] = {}
            for sym, ac_name in symbols:
                ctx = contexts[sym]
                ac = ac_configs[ac_name]
                sym_tf = timeframe_for(sym, cfg)
                start = session_start_for(cycle_now, ac)
                bars = data.get_bars(sym, ac_name, sym_tf,
                                     start=start, end=cycle_now, use_cache=False)
                last_known_ts = ctx.bars[-1].ts if ctx.bars else None
                new_bars = [b for b in bars if last_known_ts is None or b.ts > last_known_ts]
                if new_bars:
                    fresh_bars[sym] = new_bars

            engine.tick(now=cycle_now, fresh_bars=fresh_bars)

            account = alpaca.get_account()
            equity = float(account.get("equity") or account.get("portfolio_value") or ledger.equity)
            rm.update_equity(equity)
            cash_raw = (account.get("non_marginable_buying_power")
                        or account.get("cash"))
            rm.update_cash(float(cash_raw) if cash_raw is not None else None)

            daily_pnl_pct = ledger.day_pnl / initial_equity if initial_equity > 0 else 0.0

            logger.info("CYCLE_DONE equity=%.2f day_pnl=%.2f open_positions=%d",
                        equity, ledger.day_pnl, book.count())

            # Sync mutable position state (bars_held, breakeven moves) to MySQL
            # so the next cycle's rebuild sees current values.
            for pos in book.all():
                try:
                    ac = None
                    for sym_name, ac_name in symbols:
                        if sym_name == pos.symbol:
                            ac = ac_name
                            break
                    if ac:
                        mysql.sync_position_state(pos, ac)
                except Exception as exc:
                    logger.error("MYSQL_SYNC_FAILED symbol=%s: %s",
                                pos.symbol, exc, exc_info=True)

            snap = _collect_snapshot(symbols, contexts, book, ledger)
            state_file_path = os.environ.get("STATE_FILE_PATH", f"runtime/trading_state_{system_name}.json")
            write_dashboard_state(state_file_path, snap)
        except Exception as exc:
            logger.error("CYCLE_ERROR: %s", exc, exc_info=True)

    logger.info("Shutdown requested. Closing.")


if __name__ == "__main__":
    main()
