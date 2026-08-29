"""Opening Drive equity trader — production entry point.

Daily lifecycle (America/New_York):
    09:00        boot, PDT guard, baseline staleness fallback
    09:30-10:00  opening range forms; no requests issued
    10:00        scanner cut -> watchlist -> armed setups
    10:00-11:00  1-min bars on watchlist symbols; entry on trigger
    11:00-15:30  managed phase on 5-min bars
    15:30        cancel OCO children, then flatten everything
    16:10        in-process baseline refresh

Structure mirrors main_gap_and_go.py. Four deliberate differences:
  1. SectorExposureFilter is in the pipeline (portfolio concentration).
  2. loss_filter_scope is system_wide -- symbols rotate daily, so per-symbol
     counting would never fire.
  3. update_cash IS called. main_gap_and_go omits it, which leaves
     available_cash None and disables the notional cap this strategy depends
     on for its capital split with sma_slope.
  4. The PDT guard runs before any trading.
"""
from __future__ import annotations

import argparse
import logging
import os
import signal as _signal
import time as _time
from datetime import datetime, time as _dt_time, timedelta, timezone

import pytz
import yaml

from broker.alpaca_client import AlpacaClient
from broker.alpaca_data import AlpacaData
from broker.order_executor import OrderExecutor
from core.asset_class import AssetClassConfig
from core.position_manager import PositionManager
from risk.filters import (
    BrokerPositionFilter, ConcurrentPositionFilter, ConsecutiveLossFilter,
    FilterPipeline, ManualCloseCooldownFilter, NewsBlackoutFilter,
    RiskBudgetFilter, SectorExposureFilter,
)
from risk.manager import RiskManager
from risk.pdt_guard import check_pdt_headroom
from risk.sizing import SizingConfig
from scheduler.opening_drive_loop import OpeningDriveConfig, OpeningDriveLoop
from state.daily_ledger import DailyLedger
from state.mysql_store import MySQLStore
from state.position_book import PositionBook
from strategies.opening_drive_scanner import (
    OpeningDriveFilters, OpeningDriveScanner, load_baselines, load_universe,
)

logger = logging.getLogger(__name__)

_NY_TZ = pytz.timezone("America/New_York")
_shutdown = False


def _handle_shutdown(signum, frame):
    global _shutdown
    _shutdown = True


def _ny_dt(day: datetime, hh: int, mm: int) -> datetime:
    """Build a timezone-aware UTC datetime from an NY date + local HH:MM."""
    ny_date = day.astimezone(_NY_TZ).date()
    naive = datetime.combine(ny_date, _dt_time(hh, mm))
    return _NY_TZ.localize(naive).astimezone(timezone.utc)


def _sleep_until(target: datetime, sleeper) -> None:
    """Sleep in 1-second chunks until ``target``, honoring ``_shutdown``."""
    while not _shutdown:
        remaining = (target - datetime.now(timezone.utc)).total_seconds()
        if remaining <= 0:
            return
        sleeper(min(remaining, 1.0))


def load_config(path: str) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def build_pipeline(cfg: dict, sector_map: dict[str, str], alpaca=None,
                   mysql=None, strategy_id: int | None = None) -> FilterPipeline:
    """Entry-filter pipeline.

    SectorExposureFilter is what makes 'top-5 at full risk each' mean five
    independent risks rather than one leveraged sector bet.

    ConsecutiveLossFilter uses scope 'system_wide': ConsecutiveLossFilter
    branches on that exact string and treats anything else as per-symbol.
    Since this strategy rotates its symbols daily, per-symbol counting would
    never reach the limit.
    """
    filters = [
        NewsBlackoutFilter(windows=[], pad_min=5),
        ConsecutiveLossFilter(
            limit=cfg["risk"]["consecutive_loss_limit"],
            scope=cfg["risk"]["loss_filter_scope"],
        ),
        ConcurrentPositionFilter(
            max_concurrent=cfg["risk"]["max_concurrent_positions"],
        ),
        SectorExposureFilter(
            sector_map=sector_map,
            max_per_sector=cfg["risk"]["max_per_sector"],
            setup_name="opening_drive",
        ),
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
    filters.append(RiskBudgetFilter(
        daily_open_risk_cap_pct=cfg["risk"]["max_daily_risk_open"],
    ))
    return FilterPipeline(filters)


def refresh_equity_and_cash(alpaca, risk_manager) -> None:
    """Push broker equity AND available cash into the risk manager.

    The update_cash call is REQUIRED, not optional. size_position caps every
    position at available_cash * (1 - cash_buffer_pct); with available_cash
    None that cap is skipped and sizing silently falls back to equity alone,
    ignoring that sma_slope is holding 60% of the account. main_gap_and_go.py
    omits this call -- do not copy that omission.
    """
    account = alpaca.get_account()
    equity = float(account.get("equity") or account.get("portfolio_value") or 0)
    if equity > 0:
        risk_manager.update_equity(equity)
    cash_raw = (account.get("non_marginable_buying_power")
                or account.get("cash"))
    risk_manager.update_cash(float(cash_raw) if cash_raw is not None else None)


def refresh_baselines_post_close(loop, now: datetime) -> int:
    """Post-close (16:10 NY) baseline rebuild. Returns symbols written.

    Runs in-process per spec 5. An empty result does NOT overwrite the
    existing file -- a transient data outage must not erase good baselines
    and leave the next cut with nothing to screen against.
    """
    from scripts.build_opening_drive_baselines import build_baselines

    symbols = loop.scanner.request_symbols()
    built = build_baselines(
        loop.data, symbols, now,
        or_minutes=loop.cfg.or_minutes,
        lookback_sessions=loop.cfg.lookback_sessions,
    )
    if not built:
        logger.error("OD_BASELINE_REFRESH_EMPTY — keeping existing file")
        return 0
    from strategies.opening_drive_scanner import save_baselines
    save_baselines(built, loop.cfg.baselines_path)
    loop.scanner.baselines = built      # live-reload; no restart required
    logger.info("OD_BASELINE_REFRESH_DONE n=%d path=%s",
                len(built), loop.cfg.baselines_path)
    return len(built)


def build_loop(cfg: dict, log: logging.Logger):
    """Wire the loop. Returns (loop, risk_manager, alpaca_client)."""
    system_name = cfg["system"]["name"]

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

    scan_cfg = cfg["scanner"]
    setup_cfg = cfg["setups"]["opening_drive"]
    universe = load_universe(scan_cfg["universe_file"])
    baselines = load_baselines(scan_cfg["baselines_path"])
    scanner = OpeningDriveScanner(
        universe=universe,
        baselines=baselines,
        filters=OpeningDriveFilters(**scan_cfg["filters"]),
        max_concurrent_positions=cfg["risk"]["max_concurrent_positions"],
        candidate_multiplier=scan_cfg["ranking"]["candidate_multiplier"],
        baselines_max_age_days=scan_cfg["baselines_max_age_days"],
        or_minutes=scan_cfg["or_minutes"],
    )

    od_cfg = OpeningDriveConfig(
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
    )

    alpaca = AlpacaClient(asset_class="equity")
    data = AlpacaData(alpaca, cache_dir="runtime/bars_cache")

    account = alpaca.get_account()
    # PDT guard BEFORE anything else touches the market. Failing to start is
    # far cheaper than a 90-day closing-only restriction discovered mid-session.
    check_pdt_headroom(
        account,
        min_equity=float(cfg["risk"].get("pdt_min_equity", 25_000)),
        enabled=bool(cfg["risk"].get("pdt_guard_enabled", True)),
    )

    mysql = MySQLStore(strategy_name=system_name, logger=log)
    try:
        mysql.ensure_schema()
        mysql.upsert_strategy()
    except Exception as exc:
        log.error("MYSQL_INIT_FAILED %s — continuing without persistence", exc)
        mysql = None

    book = mysql.load_open_positions() if mysql else None
    if book is None:
        book = PositionBook()

    initial_equity = float(
        account.get("equity") or account.get("portfolio_value") or 0
    )
    if initial_equity <= 0:
        raise SystemExit("Account returned non-positive equity; aborting")
    _acct = str(account.get("account_number") or "")
    log.info(
        "ALPACA_ACCOUNT_BOUND asset_class=equity account_number=%s "
        "equity=%.2f base_url=%s",
        f"{_acct[:4]}***" if len(_acct) >= 4 else "***",
        initial_equity, alpaca.base_url,
    )
    ledger = DailyLedger(initial_equity=initial_equity)

    pipeline = build_pipeline(
        cfg, sector_map=universe, alpaca=alpaca, mysql=mysql,
        strategy_id=mysql.strategy_id if mysql is not None else None,
    )
    sizing = SizingConfig(
        max_risk_per_trade=cfg["risk"]["max_risk_per_trade"],
        max_notional_per_trade_pct=cfg["risk"]["max_notional_per_trade_pct"],
        allow_fractional=False,
    )
    risk_manager = RiskManager(
        pipeline=pipeline, sizing_equity=sizing, sizing_crypto=sizing,
        ledger=ledger, book=book,
    )
    executor = OrderExecutor(alpaca, book, strategy_name=system_name,
                             logger=log, mysql_store=mysql)

    def _order_status_for(pos):
        if not pos.order_id:
            return None
        order = alpaca.get_order(pos.order_id)
        return order.get("status") if isinstance(order, dict) else None

    def _on_fill_confirmed(pos):
        if mysql is not None:
            mysql.mark_fill_confirmed(mysql.strategy_id, pos.symbol, pos.setup)

    pm = PositionManager(
        book,
        max_hold_bars=cfg["position_management"]["max_hold_bars"],
        breakeven_at_R=cfg["position_management"]["breakeven_at_R"],
        order_status_for=_order_status_for,
        on_fill_confirmed=_on_fill_confirmed,
    )

    loop = OpeningDriveLoop(
        cfg=od_cfg, scanner=scanner, equity_asset_class=eq_cfg,
        alpaca_client=alpaca, alpaca_data=data,
        risk_manager=risk_manager, executor=executor, book=book,
        position_manager=pm, strategy_name=system_name,
    )
    # Seed cash/equity immediately so the very first sizing call is correct.
    refresh_equity_and_cash(alpaca, risk_manager)
    return loop, risk_manager, alpaca


def run_day(
    loop: OpeningDriveLoop,
    risk_manager,
    alpaca,
    *,
    day_anchor: datetime | None = None,
    sleeper=None,
) -> None:
    """Drive a single Opening Drive trading day through all phases.

    Phase sequence (America/New_York):
        sleep → 10:00  refresh equity/cash
        10:00          run_cut → watchlist + armed setups
        10:00–11:00    1-min bar window; on_bar per watchlist symbol
        11:00          switch_to_regular_session_bars()
        11:00–15:30    managed phase on 5-min bars; refresh equity/cash each loop
        15:30          force_close_all()
        16:10          refresh_baselines_post_close()  (in-process, no restart)

    Mirrors main_gap_and_go.py's run_day scheduling shape. Three deliberate
    differences: (a) switch_to_regular_session_bars at 11:00 (not 09:30),
    (b) refresh_equity_and_cash is called — gap_and_go omits it, (c)
    in-process baseline refresh at 16:10 so no separate compose service is
    needed.
    """
    if sleeper is None:
        sleeper = _time.sleep

    now = day_anchor or datetime.now(timezone.utc)
    cut_t = loop.cut_time(now)
    entry_end_t = loop.entry_window_end(now)
    eod_t = loop.eod_close_time(now)
    baseline_t = _ny_dt(now, 16, 10)

    # ── sleep to 10:00 (OR window forms 09:30-10:00; no requests) ─────
    _sleep_until(cut_t, sleeper)
    if _shutdown:
        return

    # ── 10:00 cut ─────────────────────────────────────────────────────
    refresh_equity_and_cash(alpaca, risk_manager)
    loop.run_cut(datetime.now(timezone.utc))

    # ── 10:00–11:00  1-min bar entry window ───────────────────────────
    last_bar_ts = cut_t - timedelta(minutes=2)
    while not _shutdown and datetime.now(timezone.utc) < entry_end_t:
        sleeper(60)
        now = datetime.now(timezone.utc)
        for r in list(loop.day.watchlist):
            sym = r.symbol
            try:
                bars = loop.data.get_bars(
                    sym, "equity", "1Min",
                    start=last_bar_ts, end=now, use_cache=False,
                )
            except Exception as exc:
                logger.error("OD_ENTRY_BARS_FAILED symbol=%s: %s", sym, exc)
                continue
            for bar in bars:
                if bar.ts > last_bar_ts:
                    loop.on_bar(sym, bar)
            if bars:
                last_bar_ts = max(last_bar_ts, max(b.ts for b in bars))

    # ── 11:00 boundary: reset contexts to accept 5-min bars ───────────
    # Must happen before manage_open receives any 5-min bars; mixing
    # timeframes corrupts ctx.atr() and ctx.vwap (same fix as gap_and_go's
    # switch_to_regular_session_bars at its 09:30 boundary).
    loop.switch_to_regular_session_bars()

    # ── 11:00–15:30  managed phase (5-min bars) ───────────────────────
    while not _shutdown and datetime.now(timezone.utc) < eod_t:
        sleeper(60)
        refresh_equity_and_cash(alpaca, risk_manager)
        now = datetime.now(timezone.utc)
        for pos in list(loop.book.all()):
            try:
                bars = loop.data.get_bars(
                    pos.symbol, "equity", "5Min",
                    start=entry_end_t, end=now, use_cache=False,
                )
            except Exception as exc:
                logger.error("OD_MGMT_BARS_FAILED symbol=%s: %s",
                             pos.symbol, exc)
                continue
            if bars:
                loop.manage_open(pos.symbol, bars[-1])

    # ── 15:30 EOD flat ─────────────────────────────────────────────────
    loop.force_close_all(datetime.now(timezone.utc))

    # ── 16:10 in-process baseline refresh ─────────────────────────────
    # The process is already running and holds a broker connection; a
    # scheduled service would add failure surface for no gain. An empty
    # rebuild does NOT overwrite existing baselines — refresh_baselines_post_close
    # guards that. On success it live-reloads loop.scanner.baselines so the
    # next cut uses current baselines without a restart.
    _sleep_until(baseline_t, sleeper)
    if not _shutdown:
        refresh_baselines_post_close(loop, datetime.now(timezone.utc))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config",
                    default="config/settings_opening_drive_equity.yaml")
    args = ap.parse_args()
    cfg = load_config(args.config)

    logging.basicConfig(
        level=getattr(logging, cfg["logging"]["level"]),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    _signal.signal(_signal.SIGTERM, _handle_shutdown)
    _signal.signal(_signal.SIGINT, _handle_shutdown)

    loop, risk_manager, alpaca = build_loop(cfg, logger)
    logger.info("OPENING_DRIVE_BOOTED strategy=%s universe=%d",
                cfg["system"]["name"], len(loop.scanner.universe))

    while not _shutdown:
        try:
            run_day(loop, risk_manager, alpaca)
            loop.reset_for_new_day()
            # Idle between day sessions — sleep ~10 minutes between checks.
            _time.sleep(600)
        except Exception as exc:
            logger.error("DAY_LOOP_ERROR: %s", exc, exc_info=True)
            _time.sleep(60)

    logger.info("Shutdown requested. Exiting.")


if __name__ == "__main__":
    main()
