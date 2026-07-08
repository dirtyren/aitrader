"""Gap-and-Go equity strategy entrypoint.

Boots the per-day Gap-and-Go lifecycle:
    03:30 → boot, validate Alpaca
    03:35 → refresh baselines if stale
    04:00 → snapshot poll loop (every snapshot_poll_seconds)
    08:30 → run scanner cut, build setups
    08:30 → entry_window_end → 1-min bar loop, on_bar(...)
    09:30 → attach OCO to pre-market fills, switch to 5-min bars
    09:30 → 15:55 → managed phase via PositionManager
    15:55 → EOD flat
    16:05 → idle until next day

This file is a thin wiring + loop driver — the per-phase logic lives in
scheduler.gap_and_go_loop.GapAndGoLoop, which is fully unit-tested.
"""
from __future__ import annotations

import logging
import os
import signal as _signal
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

import pytz

from broker.alpaca_client import AlpacaClient
from broker.alpaca_data import AlpacaData
from broker.order_executor import OrderExecutor
from core.asset_class import AssetClassConfig
from core.position_manager import PositionManager
from risk.filters import (
    BrokerPositionFilter, ConcurrentPositionFilter,
    ConsecutiveLossFilter, FilterPipeline,
    ManualCloseCooldownFilter,
    NewsBlackoutFilter, RiskBudgetFilter,
)
from risk.manager import RiskManager
from risk.sizing import SizingConfig
from scheduler.gap_and_go_loop import GapAndGoConfig, GapAndGoLoop
from state.daily_ledger import DailyLedger
from state.mysql_store import MySQLStore
from strategies.gap_scanner import (
    GapScanner, ScannerFilters, ScannerRanking,
)
from ui.logging_setup import setup_logging


_NY_TZ = pytz.timezone("America/New_York")
_shutdown = False


def _handle_shutdown(signum, frame):
    global _shutdown
    _shutdown = True


def load_config(path: str) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def _build_pipeline(cfg: dict, alpaca=None,
                    mysql=None, strategy_id: int | None = None) -> FilterPipeline:
    filters = [
        NewsBlackoutFilter(windows=[], pad_min=5),
        ConsecutiveLossFilter(limit=cfg["risk"]["consecutive_loss_limit"],
                              scope=cfg["risk"]["loss_filter_scope"]),
        ConcurrentPositionFilter(max_concurrent=cfg["risk"]["max_concurrent_positions"]),
    ]
    if alpaca is not None:
        filters.append(BrokerPositionFilter(
            broker=alpaca,
            cache_ttl_s=float(os.environ.get("BROKER_POSITION_FILTER_TTL_S", "30")),
        ))
    if mysql is not None and strategy_id is not None:
        filters.append(ManualCloseCooldownFilter(
            store=mysql, strategy_id=strategy_id,
            cache_ttl_s=float(os.environ.get("MANUAL_CLOSE_CACHE_TTL_S", "30")),
        ))
    filters.append(
        RiskBudgetFilter(daily_open_risk_cap_pct=cfg["risk"]["max_daily_risk_open"]),
    )
    return FilterPipeline(filters)


def _build_loop(cfg: dict, logger: logging.Logger) -> GapAndGoLoop:
    system_name = cfg["system"]["name"]

    # Asset class config (single equity entry; symbols are dynamic).
    eq_raw = cfg["asset_classes"]["equity"]
    eq_cfg = AssetClassConfig(
        name="equity", timezone=eq_raw["timezone"],
        session_open_local=eq_raw["session_open_local"],
        session_close_local=eq_raw["session_close_local"],
        opening_blackout_min=0,
        bar_timeframe=cfg["scheduler"]["bar_timeframe"],
        slippage_bps=eq_raw.get("slippage_bps", 0.0),
        commission_per_share=eq_raw.get("commission_per_share", 0.0),
        commission_bps=eq_raw.get("commission_bps", 0.0),
    )

    # Scanner.
    scan_cfg = cfg["scanner"]
    universe = GapScanner.load_universe(scan_cfg["universe_file"])
    baselines = GapScanner.load_baselines(scan_cfg["baselines_path"])
    scanner = GapScanner(
        universe=universe,
        baselines=baselines,
        baselines_max_age_days=scan_cfg["baselines_max_age_days"],
        filters=ScannerFilters(**scan_cfg["filters"]),
        ranking=ScannerRanking(
            candidate_multiplier=scan_cfg["ranking"]["candidate_multiplier"],
        ),
        max_concurrent_positions=cfg["risk"]["max_concurrent_positions"],
    )

    setup_cfg = cfg["setups"]["gap_and_go"]
    gg_cfg = GapAndGoConfig(
        universe_path=scan_cfg["universe_file"],
        baselines_path=scan_cfg["baselines_path"],
        baselines_max_age_days=scan_cfg["baselines_max_age_days"],
        snapshot_poll_seconds=scan_cfg["snapshot_poll_seconds"],
        atr_mult_stop_cap=setup_cfg["atr_mult_stop_cap"],
        target_R=setup_cfg["target_R"],
        volume_confirm_mult=setup_cfg["volume_confirm_mult"],
        max_entry_slippage_pct=setup_cfg["max_entry_slippage_pct"],
        entry_window_minutes=setup_cfg["entry_window_minutes"],
        premarket_bar_timeframe=cfg["scheduler"]["bar_timeframe"],
        regular_bar_timeframe=cfg["scheduler"]["regular_session_timeframe"],
        max_concurrent_positions=cfg["risk"]["max_concurrent_positions"],
    )

    # Broker + persistence.
    asset_class = next(iter(cfg["asset_classes"].keys()))
    alpaca = AlpacaClient(asset_class=asset_class)
    data = AlpacaData(alpaca, cache_dir="runtime/bars_cache")
    mysql = MySQLStore(strategy_name=system_name, logger=logger)
    try:
        mysql.ensure_schema()
        mysql.upsert_strategy()
    except Exception as exc:
        logger.error("MYSQL_INIT_FAILED %s — continuing without persistence", exc)
        mysql = None

    book = mysql.load_open_positions() if mysql else None
    if book is None:
        from state.position_book import PositionBook
        book = PositionBook()

    account = alpaca.get_account()
    initial_equity = float(account.get("equity") or account.get("portfolio_value") or 0)
    if initial_equity <= 0:
        raise SystemExit("Account returned non-positive equity; aborting")
    _acct_num = str(account.get("account_number") or "")
    _acct_masked = f"{_acct_num[:4]}***" if len(_acct_num) >= 4 else "***"
    logger.info(
        "ALPACA_ACCOUNT_BOUND asset_class=%s account_number=%s equity=%.2f base_url=%s",
        asset_class, _acct_masked, initial_equity, alpaca.base_url,
    )
    ledger = DailyLedger(initial_equity=initial_equity)

    pipeline = _build_pipeline(
        cfg, alpaca=alpaca, mysql=mysql,
        strategy_id=mysql.strategy_id if mysql is not None else None,
    )

    sizing = SizingConfig(
        max_risk_per_trade=cfg["risk"]["max_risk_per_trade"],
        max_notional_per_trade_pct=cfg["risk"]["max_notional_per_trade_pct"],
        allow_fractional=False,
    )
    risk_manager = RiskManager(
        pipeline=pipeline,
        sizing_equity=sizing, sizing_crypto=sizing,
        ledger=ledger, book=book,
    )

    executor = OrderExecutor(alpaca, book, strategy_name=system_name,
                             logger=logger, mysql_store=mysql)
    def _order_status_for(pos):
        if not pos.order_id:
            return None
        order = alpaca.get_order(pos.order_id)
        if not isinstance(order, dict):
            return None
        return order.get("status")

    def _on_fill_confirmed(pos):
        if mysql is None:
            return
        mysql.mark_fill_confirmed(mysql.strategy_id, pos.symbol, pos.setup)

    pm = PositionManager(
        book,
        max_hold_bars=cfg["position_management"]["max_hold_bars"],
        breakeven_at_R=cfg["position_management"]["breakeven_at_R"],
        order_status_for=_order_status_for,
        on_fill_confirmed=_on_fill_confirmed,
    )

    return GapAndGoLoop(
        cfg=gg_cfg, scanner=scanner, equity_asset_class=eq_cfg,
        alpaca_client=alpaca, alpaca_data=data,
        risk_manager=risk_manager, executor=executor, book=book,
        position_manager=pm, ledger=ledger,
        strategy_name=system_name,
    )


def _now_ny(now_utc: datetime) -> datetime:
    return now_utc.astimezone(_NY_TZ)


def _fetch_premarket_bars(loop: GapAndGoLoop, now: datetime,
                          since: datetime) -> dict:
    """Pull 1-min bars on the live watchlist between ``since`` and ``now``."""
    out: dict = {}
    for sym in list(loop.day.setups.keys()):
        try:
            bars = loop.data.get_bars(sym, "equity", "1Min",
                                      start=since, end=now, use_cache=False)
        except Exception as exc:
            logger.error("GAPGO_PREMKT_BARS_FAILED symbol=%s: %s", sym, exc)
            continue
        if bars:
            out[sym] = bars
    return out


def run_day(loop: GapAndGoLoop, *, day_anchor: datetime | None = None,
            sleeper=time.sleep) -> None:
    """Drive a single trading day through every phase.

    ``day_anchor`` defaults to ``datetime.now(UTC)``; tests can pin it.
    """
    now = day_anchor or datetime.now(timezone.utc)
    cut_t = loop.cut_time(now)
    deadline_t = loop.deadline_time(now)
    open_t = loop.regular_open_time(now)
    eod_t = loop.eod_close_time(now)

    # 03:35 — baseline refresh if stale.
    loop.refresh_baselines_if_stale(now)
    if loop.scanner.baselines_too_old_to_trade(now):
        logger.error("Aborting day — baselines too stale to trade safely.")
        return

    # 04:00 → 08:30 — snapshot poll loop.
    poll_until = cut_t
    last_poll = now
    while not _shutdown and datetime.now(timezone.utc) < poll_until:
        loop.poll_snapshot(datetime.now(timezone.utc))
        sleeper(loop.cfg.snapshot_poll_seconds)

    # 08:30 — cut.
    loop.run_cut(datetime.now(timezone.utc))

    # 08:30 → 09:30 — 1-min bar window.
    last_bar_ts = cut_t - timedelta(minutes=2)
    while not _shutdown and datetime.now(timezone.utc) < deadline_t:
        sleeper(60)
        now = datetime.now(timezone.utc)
        bars_per_symbol = _fetch_premarket_bars(loop, now, last_bar_ts)
        for sym, bars in bars_per_symbol.items():
            for bar in bars:
                if bar.ts > last_bar_ts:
                    loop.on_bar(sym, bar)
            if bars:
                last_bar_ts = max(last_bar_ts, max(b.ts for b in bars))

    # 09:30 — OCO attach + timeframe switch.
    loop.attach_premarket_brackets(datetime.now(timezone.utc))
    loop.switch_to_regular_session_bars()

    # 09:30 → 15:55 — managed phase (5-min bars).
    while not _shutdown and datetime.now(timezone.utc) < eod_t:
        sleeper(60)
        now = datetime.now(timezone.utc)
        for pos in list(loop.book.all()):
            try:
                bars = loop.data.get_bars(pos.symbol, "equity", "5Min",
                                          start=open_t, end=now,
                                          use_cache=False)
            except Exception as exc:
                logger.error("GAPGO_MGMT_BARS_FAILED symbol=%s: %s",
                             pos.symbol, exc)
                continue
            if bars:
                loop.manage_open(pos.symbol, bars[-1])

    # 15:55 — EOD flat.
    loop.force_close_all(datetime.now(timezone.utc))


def main() -> None:
    if "--config" not in sys.argv:
        raise SystemExit("main_gap_and_go.py requires --config <yaml>")
    idx = sys.argv.index("--config")
    if idx + 1 >= len(sys.argv):
        raise SystemExit("--config requires a path argument")
    config_path = sys.argv[idx + 1]

    cfg = load_config(config_path)
    system_name = cfg["system"]["name"]
    global logger
    logger = setup_logging(log_file=cfg["logging"]["log_file"],
                           logger_name=system_name)
    logger.info("%s starting up; env=%s", system_name, cfg["system"]["trading_env"])

    _signal.signal(_signal.SIGTERM, _handle_shutdown)
    _signal.signal(_signal.SIGINT, _handle_shutdown)

    loop = _build_loop(cfg, logger)

    while not _shutdown:
        try:
            run_day(loop)
            loop.reset_for_new_day()
            # Idle until next 03:30 boot — sleep ~10 minutes between checks.
            time.sleep(600)
        except Exception as exc:
            logger.error("DAY_LOOP_ERROR: %s", exc, exc_info=True)
            time.sleep(60)

    logger.info("Shutdown requested. Exiting.")


if __name__ == "__main__":
    main()
