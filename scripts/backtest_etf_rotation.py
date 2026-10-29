#!/usr/bin/env python3
"""
Dual-Momentum Tactical ETF Rotation Strategy Backtest.

Strategy Architecture & Universe:
- Satellite Candidates: SMGB, QDVE, EQQQ, ISPY, XUFN, SXLE, XUIN (or US proxies: SMH, XLK, QQQ, CIBR, XLF, XLE, XLI).
- Safe Harbor: CSH2 / FEDF / XEON / BIL (Cash equivalent).
- Macro Trend Filter: S&P 500 Index (SPY) vs its 200-day Simple Moving Average (SMA).
- Volatility Circuit Breaker: CBOE VIX Index.

Quantitative Rules:
1. Relative Momentum Score: MomentumScore = 0.60 * R12W + 0.40 * R24W (60-day & 120-day returns).
2. Trimester Rebalance: Last Friday of March, June, September, December.
   - S&P 500 > 200 SMA and VIX < 22: Top 1 (35%), Top 2 (35%), Top 3 (20%), Cash (10%).
   - S&P 500 > 200 SMA and 22 <= VIX < 32: Top 1 (25%), Top 2 (25%), Cash (50%).
   - S&P 500 > 200 SMA and VIX >= 32: 100% Cash.
   - S&P 500 <= 200 SMA: 100% Cash.
3. Turnover Buffer Rule: If #3 and #4 differ by < 1.0% (0.01), retain incumbent.
4. Weekly Risk-Off Check: Every Friday at market close.
   - S&P 500 closed below 200d SMA for 2 consecutive trading days OR VIX >= 32 -> Exit all equities to 100% Cash.
5. Re-Entry Hysteresis Rule: Require S&P 500 to close above 200d SMA for 3 consecutive days AND VIX < 25.

Run:
    python scripts/backtest_etf_rotation.py --synthetic
    python scripts/backtest_etf_rotation.py --start 2018-01-01 --end 2026-08-28 --use-us-proxies
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

# Ensure repository root is on sys.path
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import numpy as np
import pandas as pd
import requests
from dotenv import load_dotenv

from strategies.dual_momentum_etf import (
    DualMomentumConfig,
    DualMomentumEngine,
    RegimeState,
    RebalanceTrigger,
    compute_sma_200,
    is_friday,
    is_trimester_rebalance_date,
)

# ---------------------------------------------------------------------------
# Default Settings
# ---------------------------------------------------------------------------

START_DATE = "2018-01-01"
END_DATE = "2026-08-28"
INITIAL_CAPITAL = 100_000.0
SLIPPAGE_BPS = 2.0
FEE_RATE = 0.0
COMMISSION_PER_SHARE = 0.0
CACHE_DIR = "runtime/bars_cache"

# Satellite universe tickers (UCITS default)
UCITS_SATELLITES = ["SMGB", "QDVE", "EQQQ", "ISPY", "XUFN", "SXLE", "XUIN"]
# US ETF proxies (available on all US brokers and feeds)
US_PROXY_SATELLITES = ["SMH", "XLK", "QQQ", "CIBR", "XLF", "XLE", "XLI"]

BENCHMARK_SYMBOL = "SPY"
SAFE_HARBOR_SYMBOL = "CSH2"
US_SAFE_HARBOR_SYMBOL = "BIL"
VIX_SYMBOL = "VIX"


def _load_credentials() -> tuple[str, str]:
    for candidate in ("config/.env", ".env"):
        if os.path.exists(candidate):
            load_dotenv(candidate, override=False)
            break
    else:
        load_dotenv(override=False)
    key = os.environ.get("ALPACA_EQUITY_API_KEY", "")
    secret = os.environ.get("ALPACA_EQUITY_SECRET_KEY", "")
    return key, secret


def fetch_alpaca_daily_bars(symbol: str, start: str, end: str) -> pd.DataFrame:
    """Fetch daily bars from Alpaca if credentials exist."""
    key, secret = _load_credentials()
    if not key or not secret:
        raise RuntimeError("Missing Alpaca equity credentials in config/.env")

    url = f"https://data.alpaca.markets/v2/stocks/{symbol}/bars"
    headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}
    params = {
        "timeframe": "1Day",
        "start": f"{start}T00:00:00Z",
        "end": f"{end}T00:00:00Z",
        "limit": 10000,
        "adjustment": "split",
        "feed": "sip",
    }
    bars = []
    for _ in range(50):
        resp = requests.get(url, params=params, headers=headers, timeout=30)
        if resp.status_code != 200:
            raise RuntimeError(f"Alpaca fetch failed for {symbol}: {resp.status_code}")
        body = resp.json()
        page = body.get("bars") or []
        bars.extend(page)
        token = body.get("next_page_token")
        if not token:
            break
        params["page_token"] = token

    if not bars:
        raise RuntimeError(f"No bars returned for {symbol}")

    df = pd.DataFrame(bars)
    df = df.rename(columns={"t": "ts", "o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"})
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    df = df.sort_values("ts").reset_index(drop=True)
    return df[["ts", "open", "high", "low", "close", "volume"]]


def generate_synthetic_data(
    satellites: list[str],
    benchmark_symbol: str,
    safe_harbor_symbol: str,
    start: str,
    end: str,
) -> tuple[pd.DataFrame, pd.Series, pd.Series]:
    """Generate realistic synthetic market data for offline backtesting and CI testing.

    Includes multi-year bull market, correction/bear market (crossing 200 SMA),
    VIX spikes >= 32, and subsequent recoveries.
    """
    np.random.seed(42)
    dates = pd.date_range(start=start, end=end, freq="B")
    n = len(dates)

    # 1. Benchmark (SPY) with trend and drawdowns
    trend = np.linspace(250.0, 520.0, n)
    # Add a bear market in the middle (e.g. 2020 or 2022 style drawdown)
    bear_mask = np.zeros(n)
    start_bear = int(n * 0.35)
    end_bear = int(n * 0.50)
    bear_mask[start_bear:end_bear] = -np.sin(np.linspace(0, np.pi, end_bear - start_bear)) * 80.0

    noise = np.cumsum(np.random.normal(0, 1.2, n))
    bench_close = trend + bear_mask + noise
    bench_series = pd.Series(bench_close, index=dates, name=benchmark_symbol)

    # 2. VIX series (elevated during the bear period)
    vix_base = 15.0 + np.random.normal(0, 1.5, n).clip(-4, 6)
    for i in range(start_bear, end_bear):
        vix_base[i] += 18.0 + np.random.normal(0, 3.0)  # VIX > 32 during bear
    vix_series = pd.Series(vix_base.clip(10.0, 55.0), index=dates, name="VIX")

    # 3. Satellite ETFs with different momentum profiles
    prices = {}
    for i, sym in enumerate(satellites):
        beta = 1.0 + (i * 0.15)
        idio = np.cumsum(np.random.normal(0, 0.8, n))
        p = (bench_close * (beta * 0.4)) + 50.0 + idio
        prices[sym] = pd.Series(p.clip(10.0), index=dates)

    # 4. Safe harbor asset (slight positive drift, e.g. overnight rate)
    cash_ret = 0.035 / 252.0  # 3.5% annualized risk-free rate
    cash_price = 100.0 * np.cumprod(1.0 + np.full(n, cash_ret))
    prices[safe_harbor_symbol] = pd.Series(cash_price, index=dates)

    prices_df = pd.DataFrame(prices, index=dates)
    return prices_df, bench_series, vix_series


# ---------------------------------------------------------------------------
# Simulation Engine
# ---------------------------------------------------------------------------

def run_simulation(
    prices_df: pd.DataFrame,
    benchmark_series: pd.Series,
    vix_series: pd.Series,
    cfg: DualMomentumConfig,
    initial_capital: float = INITIAL_CAPITAL,
    slippage_bps: float = SLIPPAGE_BPS,
    fee_rate: float = FEE_RATE,
) -> dict:
    """Run event-driven daily historical simulation with no lookahead.

    Fills occur at next-day open or close with slippage applied.
    """
    engine = DualMomentumEngine(cfg)
    dates = prices_df.index
    n = len(dates)

    # Benchmark 200 SMA
    bench_sma = compute_sma_200(benchmark_series, period=cfg.sma_period)

    # Mark-to-market portfolio state
    cash = initial_capital
    holdings: dict[str, float] = {col: 0.0 for col in prices_df.columns}

    equity_curve = []
    rebalance_log = []
    circuit_breaker_events = 0
    trimester_rebalance_events = 0

    slip = slippage_bps / 10000.0

    # Start simulation once warm-up period is reached
    warmup_bars = max(cfg.sma_period, cfg.lookback_24w + 1)

    for i in range(warmup_bars, n):
        cur_date = dates[i].date() if hasattr(dates[i], "date") else dates[i]
        prior_slice = prices_df.iloc[:i]

        bench_c = float(benchmark_series.iloc[i - 1])
        bench_s = float(bench_sma.iloc[i - 1])
        vix_val = float(vix_series.iloc[i - 1])

        # Current bar prices
        today_prices = {col: float(prices_df[col].iloc[i]) for col in prices_df.columns}

        # Step evaluation on previous completed day's close
        step_res = engine.evaluate_step(
            eval_date=cur_date,
            prices_history=prior_slice,
            benchmark_close=bench_c,
            benchmark_sma=bench_s,
            vix_value=vix_val,
        )

        if step_res.rebalance_triggered:
            if step_res.trigger_type == RebalanceTrigger.CIRCUIT_BREAKER:
                circuit_breaker_events += 1
            elif step_res.trigger_type == RebalanceTrigger.TRIMESTER:
                trimester_rebalance_events += 1

            # Mark to market before rebalance
            current_equity = cash + sum(holdings[sym] * today_prices[sym] for sym in holdings)

            target_weights = step_res.target_weights

            # Target shares per symbol
            target_holdings: dict[str, float] = {}
            for sym, weight in target_weights.items():
                px = today_prices.get(sym, 0.0)
                if px > 0 and weight > 0:
                    allocated_dollars = current_equity * weight
                    target_holdings[sym] = allocated_dollars / px
                else:
                    target_holdings[sym] = 0.0

            # Execute SELLS first
            turnover_dollars = 0.0
            for sym, cur_qty in holdings.items():
                tgt_qty = target_holdings.get(sym, 0.0)
                if cur_qty > tgt_qty:
                    sell_qty = cur_qty - tgt_qty
                    sell_px = today_prices[sym] * (1.0 - slip)  # adverse slippage
                    proceeds = sell_qty * sell_px
                    fee = proceeds * fee_rate
                    cash += proceeds - fee
                    holdings[sym] = tgt_qty
                    turnover_dollars += proceeds

            # Execute BUYS next
            for sym, tgt_qty in target_holdings.items():
                cur_qty = holdings.get(sym, 0.0)
                if tgt_qty > cur_qty:
                    buy_qty = tgt_qty - cur_qty
                    buy_px = today_prices[sym] * (1.0 + slip)  # adverse slippage
                    cost = buy_qty * buy_px
                    fee = cost * fee_rate
                    if cash >= cost + fee:
                        cash -= (cost + fee)
                        holdings[sym] = tgt_qty
                        turnover_dollars += cost

            rebalance_log.append({
                "date": cur_date,
                "trigger": step_res.trigger_type.value,
                "regime": step_res.regime.value,
                "vix": vix_val,
                "equity": current_equity,
                "turnover": turnover_dollars,
                "holdings": {s: round(q, 2) for s, q in holdings.items() if q > 0},
                "reason": step_res.reason,
            })

        # Daily mark-to-market
        daily_equity = cash + sum(holdings[sym] * today_prices[sym] for sym in holdings)
        equity_curve.append({
            "date": dates[i],
            "equity": daily_equity,
            "cash": cash,
            "regime": engine.regime.value,
            "benchmark": float(benchmark_series.iloc[i]),
        })

    eq_df = pd.DataFrame(equity_curve).set_index("date")
    return {
        "equity_curve": eq_df,
        "rebalance_log": pd.DataFrame(rebalance_log),
        "circuit_breaker_events": circuit_breaker_events,
        "trimester_rebalance_events": trimester_rebalance_events,
    }


def compute_metrics(equity_curve: pd.DataFrame, initial_capital: float) -> dict:
    eq = equity_curve["equity"]
    final_equity = float(eq.iloc[-1])
    total_return_pct = ((final_equity / initial_capital) - 1.0) * 100.0

    n_days = len(eq)
    years = n_days / 252.0
    cagr_pct = (((final_equity / initial_capital) ** (1.0 / years)) - 1.0) * 100.0 if years > 0 else 0.0

    daily_ret = eq.pct_change().dropna()
    vol_annual = float(daily_ret.std() * (252.0 ** 0.5) * 100.0)

    # Sharpe (assuming 2% risk-free rate)
    rf_daily = 0.02 / 252.0
    excess_ret = daily_ret - rf_daily
    sharpe = float((excess_ret.mean() / daily_ret.std()) * (252.0 ** 0.5)) if daily_ret.std() > 0 else 0.0

    # Sortino (downside deviation)
    downside = daily_ret[daily_ret < 0]
    downside_std = float(downside.std() * (252.0 ** 0.5)) if len(downside) > 0 else 1e-6
    sortino = float(((daily_ret.mean() * 252.0) - 0.02) / downside_std) if downside_std > 0 else 0.0

    # Max Drawdown
    peak = eq.cummax()
    drawdown = (eq - peak) / peak
    max_dd_pct = float(drawdown.min() * 100.0)

    # Calmar ratio
    calmar = float(cagr_pct / abs(max_dd_pct)) if max_dd_pct != 0 else 0.0

    # Benchmark metrics
    bench = equity_curve["benchmark"]
    bench_final = float(bench.iloc[-1])
    bench_initial = float(bench.iloc[0])
    bench_ret_pct = ((bench_final / bench_initial) - 1.0) * 100.0
    bench_cagr_pct = (((bench_final / bench_initial) ** (1.0 / years)) - 1.0) * 100.0 if years > 0 else 0.0
    bench_peak = bench.cummax()
    bench_max_dd_pct = float(((bench - bench_peak) / bench_peak).min() * 100.0)

    return {
        "final_equity": final_equity,
        "total_return_pct": total_return_pct,
        "cagr_pct": cagr_pct,
        "vol_annual_pct": vol_annual,
        "sharpe": sharpe,
        "sortino": sortino,
        "max_dd_pct": max_dd_pct,
        "calmar": calmar,
        "bench_total_return_pct": bench_ret_pct,
        "bench_cagr_pct": bench_cagr_pct,
        "bench_max_dd_pct": bench_max_dd_pct,
    }


def render_report(metrics: dict, sim_res: dict, satellites: list[str]) -> str:
    lines = [
        "=" * 68,
        " DUAL-MOMENTUM TACTICAL ETF ROTATION STRATEGY REPORT",
        "=" * 68,
        f" Universe Candidates: {', '.join(satellites)}",
        "-" * 68,
        " PERFORMANCE SUMMARY",
        f"   Starting Capital  : ${INITIAL_CAPITAL:,.2f}",
        f"   Final Equity      : ${metrics['final_equity']:,.2f}",
        f"   Total Return      : {metrics['total_return_pct']:+.2f}%",
        f"   CAGR              : {metrics['cagr_pct']:+.2f}%",
        f"   Annualized Vol    : {metrics['vol_annual_pct']:.2f}%",
        f"   Sharpe Ratio      : {metrics['sharpe']:.2f}",
        f"   Sortino Ratio     : {metrics['sortino']:.2f}",
        f"   Max Drawdown      : {metrics['max_dd_pct']:.2f}%",
        f"   Calmar Ratio      : {metrics['calmar']:.2f}",
        "-" * 68,
        " RISK-OFF & REBALANCE ACTIVITY",
        f"   Trimester Rebalances    : {sim_res['trimester_rebalance_events']}",
        f"   Circuit Breaker Exits   : {sim_res['circuit_breaker_events']}",
        "-" * 68,
        " BENCHMARK (S&P 500 BUY & HOLD)",
        f"   Total Return      : {metrics['bench_total_return_pct']:+.2f}%",
        f"   CAGR              : {metrics['bench_cagr_pct']:+.2f}%",
        f"   Max Drawdown      : {metrics['bench_max_dd_pct']:.2f}%",
        "=" * 68,
    ]
    rebalance_log = sim_res["rebalance_log"]
    if not rebalance_log.empty:
        lines.append(" RECENT REBALANCE EVENTS (Last 8)")
        for _, r in rebalance_log.tail(8).iterrows():
            holdings_str = ", ".join(f"{s}:{q}" for s, q in r["holdings"].items())
            lines.append(
                f"   {r['date']} | {r['trigger']:<24} | VIX={r['vix']:.1f} | "
                f"Eq=${r['equity']:,.0f} | Holdings: [{holdings_str}]"
            )
        lines.append("=" * 68)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI Entry Point
# ---------------------------------------------------------------------------

def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Dual-Momentum ETF Rotation Backtest.")
    p.add_argument("--start", default=START_DATE)
    p.add_argument("--end", default=END_DATE)
    p.add_argument("--capital", type=float, default=INITIAL_CAPITAL)
    p.add_argument("--slippage-bps", type=float, default=SLIPPAGE_BPS)
    p.add_argument("--fee-rate", type=float, default=FEE_RATE)
    p.add_argument("--use-us-proxies", action="store_true", help="Use US ETF proxies (SMH, XLK, QQQ, etc.)")
    p.add_argument("--synthetic", action="store_true", help="Run with synthetic historical market data")
    p.add_argument("--json", action="store_true", help="Dump metrics JSON to runtime/")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])

    satellites = US_PROXY_SATELLITES if args.use_us_proxies else UCITS_SATELLITES
    safe_harbor = US_SAFE_HARBOR_SYMBOL if args.use_us_proxies else SAFE_HARBOR_SYMBOL

    cfg = DualMomentumConfig(
        satellite_symbols=satellites,
        safe_harbor_symbol=safe_harbor,
        benchmark_symbol=BENCHMARK_SYMBOL,
        vix_symbol=VIX_SYMBOL,
    )

    if args.synthetic:
        prices_df, bench_series, vix_series = generate_synthetic_data(
            satellites, BENCHMARK_SYMBOL, safe_harbor, args.start, args.end
        )
    else:
        try:
            # Attempt to fetch bars from Alpaca cache/API
            data_dict = {}
            for s in satellites + [BENCHMARK_SYMBOL, safe_harbor]:
                df_bar = fetch_alpaca_daily_bars(s, args.start, args.end)
                data_dict[s] = df_bar.set_index("ts")["close"]
            prices_df = pd.DataFrame({s: data_dict[s] for s in satellites + [safe_harbor]}).dropna()
            bench_series = data_dict[BENCHMARK_SYMBOL].reindex(prices_df.index)
            # Fetch VIX or approximate from benchmark volatility
            vix_ret = bench_series.pct_change().rolling(20).std() * (252.0 ** 0.5) * 100.0
            vix_series = vix_ret.bfill()
        except Exception as exc:
            print(f"Notice: Live data fetch failed ({exc}). Falling back to synthetic simulation mode.")
            prices_df, bench_series, vix_series = generate_synthetic_data(
                satellites, BENCHMARK_SYMBOL, safe_harbor, args.start, args.end
            )

    sim_res = run_simulation(
        prices_df=prices_df,
        benchmark_series=bench_series,
        vix_series=vix_series,
        cfg=cfg,
        initial_capital=args.capital,
        slippage_bps=args.slippage_bps,
        fee_rate=args.fee_rate,
    )

    metrics = compute_metrics(sim_res["equity_curve"], args.capital)
    print(render_report(metrics, sim_res, satellites))

    if args.json:
        os.makedirs("runtime", exist_ok=True)
        out_path = "runtime/etf_rotation_backtest_metrics.json"
        with open(out_path, "w") as f:
            json.dump({**metrics, "circuit_breakers": sim_res["circuit_breaker_events"]}, f, indent=2)
        print(f"Metrics written to {out_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
