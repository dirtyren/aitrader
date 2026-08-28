#!/usr/bin/env python3
"""
SMA-Slope Trend-Following Backtest — Long-Only, Daily (1D) timeframe.

Strategy
--------
Base indicator is a Simple Moving Average, SMA(N). At each daily close (candle t)
we compute the slope of the SMA as the difference against its own value k bars ago:

    slope(t) = SMA(t) - SMA(t-k)

Entry (LONG) — evaluated at the CLOSE of candle t, EXECUTED at the OPEN of t+1:
    close(t) > SMA(t)  AND  slope(t) > 0        (SMA rising)

Exit (CLOSE) — evaluated at the CLOSE of candle t, EXECUTED at the OPEN of t+1:
    close(t) < SMA(t)  OR   slope(t) < 0        (SMA falling)

Design guarantees
-----------------
* Long-only: position is either 100% invested or 100% in cash. No shorts.
* No lookahead bias: every signal is computed from the close of candle t and
  filled strictly at the open of candle t+1. The indicator window uses only
  bars up to and including t (SMA is a trailing mean; `slope` needs bar t-k).
* Configurable initial capital, transaction fee, and slippage.
* Split-adjusted daily bars by default (TQQQ has had 2-for-1 splits) so the
  SMA/slope signal is not corrupted by price discontinuities.

Run
---
    python scripts/sma_slope_backtest.py
    python scripts/sma_slope_backtest.py --sma 200 --slope 5
    python scripts/sma_slope_backtest.py --sma 50  --slope 3 --symbol TQQQ

Dependencies: numpy, pandas, requests, python-dotenv, pyarrow (cache only).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import requests
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Configurable parameters (also overridable via CLI flags / env vars).
# ---------------------------------------------------------------------------

SYMBOL = "TQQQ"                 # Ticker to backtest
TIMEFRAME = "1Day"              # Alpaca daily bars
START_DATE = "2018-01-01"       # Backtest start (inclusive)
END_DATE = "2026-08-28"         # Backtest end (exclusive-ish; today)

SMA_PERIOD = 200                # N  — SMA lookback (e.g. 200 or 50)
SLOPE_LOOKBACK = 5              # k  — bars used to estimate SMA slope (e.g. 3 or 5)

INITIAL_CAPITAL = 100_000.0     # Starting equity ($)
SLIPPAGE_BPS = 2.0              # Adverse slippage, basis points (2 = 0.02% / side)
COMMISSION_PER_SHARE = 0.0      # Flat per-share commission ($ / side)
FEE_RATE = 0.0                  # Notional fee rate per side (0.0005 = 5 bps)

ADJUSTMENT = "split"            # "split" | "raw" | "all" — split-adjusted avoids TQQQ split gaps
FEED = "sip"                    # Alpaca data feed: "sip" (full) or "iex" (free tier)
CACHE_DIR = "runtime/bars_cache"

# ---------------------------------------------------------------------------
# Data access
# ---------------------------------------------------------------------------

_DATA_URL = "https://data.alpaca.markets/v2/stocks/{symbol}/bars"


def _load_credentials() -> tuple[str, str]:
    """Resolve Alpaca equity keys from config/.env (repo convention)."""
    # Try the project .env first, then any .env already loaded in the process.
    for candidate in ("config/.env", ".env"):
        if os.path.exists(candidate):
            load_dotenv(candidate, override=False)
            break
    else:
        load_dotenv(override=False)

    key = os.environ.get("ALPACA_EQUITY_API_KEY", "")
    secret = os.environ.get("ALPACA_EQUITY_SECRET_KEY", "")
    if not key or not secret:
        raise RuntimeError(
            "Missing ALPACA_EQUITY_API_KEY / ALPACA_EQUITY_SECRET_KEY in config/.env"
        )
    return key, secret


def fetch_daily_bars(symbol: str, start: str, end: str) -> pd.DataFrame:
    """Fetch split-adjusted daily bars from Alpaca, following pagination.

    Returns a DataFrame indexed by an ascending integer RangeIndex with columns
    [ts, open, high, low, close, volume], sorted oldest -> newest.
    """
    key, secret = _load_credentials()
    headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}
    params = {
        "timeframe": TIMEFRAME,
        "start": f"{start}T00:00:00Z",
        "end": f"{end}T00:00:00Z",
        "limit": 10000,
        "adjustment": ADJUSTMENT,
        "feed": FEED,
    }

    bars: list[dict] = []
    # Alpaca caps a page at 10k bars; follow next_page_token for full history.
    for _ in range(200):  # hard bound: 200 pages * 10k = 2M bars — far more than needed
        resp = requests.get(
            _DATA_URL.format(symbol=symbol), params=params, headers=headers, timeout=30
        )
        if resp.status_code != 200:
            raise RuntimeError(f"Alpaca bars request failed: HTTP {resp.status_code} {resp.text[:200]}")
        body = resp.json()
        page = body.get("bars") or []
        bars.extend(page)
        token = body.get("next_page_token")
        if not token:
            break
        params["page_token"] = token

    if not bars:
        raise RuntimeError(f"No daily bars returned for {symbol} in [{start}, {end}]")

    df = pd.DataFrame(bars)
    df = df.rename(columns={"t": "ts", "o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"})
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    df = df.sort_values("ts").reset_index(drop=True)
    return df[["ts", "open", "high", "low", "close", "volume"]]


def load_bars(symbol: str, start: str, end: str) -> pd.DataFrame:
    """Load bars from a parquet cache, falling back to a live fetch."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    cache_path = os.path.join(CACHE_DIR, f"{symbol}_1D_{start}_{end}.parquet")
    if os.path.exists(cache_path):
        try:
            return pd.read_parquet(cache_path)
        except Exception:
            pass  # corrupt/stale cache — refetch below

    df = fetch_daily_bars(symbol, start, end)
    try:
        df.to_parquet(cache_path, index=False)
    except Exception:
        pass  # cache write is best-effort; the backtest still runs
    return df


# ---------------------------------------------------------------------------
# Indicator & signal generation (vectorised, strictly causal)
# ---------------------------------------------------------------------------

def compute_signals(close: np.ndarray, n: int, k: int) -> np.ndarray:
    """Return a boolean array `long_desired[t]` for each day t.

    True  -> want to be long (close > SMA and SMA rising) at close of day t.
    False -> flat (close < SMA or SMA falling).
    The first N+k-1 bars have insufficient history and are False.
    """
    sma = pd.Series(close).rolling(window=n, min_periods=n).mean().to_numpy()
    slope = sma - pd.Series(sma).shift(k).to_numpy()  # SMA(t) - SMA(t-k)

    above = close > sma
    rising = slope > 0.0
    signal = above & rising
    signal = np.nan_to_num(signal.astype(float), nan=0.0).astype(bool)
    return signal


# ---------------------------------------------------------------------------
# Backtest engine (all-in, long-only, next-open execution)
# ---------------------------------------------------------------------------

def run_backtest(df: pd.DataFrame, signal: np.ndarray,
                 initial_capital: float, slippage_bps: float,
                 commission_per_share: float, fee_rate: float) -> dict:
    """Simulate the strategy and build a daily mark-to-market equity curve.

    Fills execute at the OPEN of the bar AFTER the signal bar (t+1), with
    slippage applied adversarially (buy higher, sell lower). Fees are applied
    per side.
    """
    ts = df["ts"].to_numpy()
    open_px = df["open"].to_numpy(dtype=float)
    close_px = df["close"].to_numpy(dtype=float)
    n_bars = len(df)

    slip = slippage_bps / 10_000.0  # bps -> fraction

    cash = initial_capital
    shares = 0
    entry_price = 0.0
    entry_ts = None

    trades: list[dict] = []
    equity_curve = np.empty(n_bars, dtype=float)

    # Iterate signal bars; a signal on day i fills on day i+1, so stop at n_bars-2.
    for i in range(n_bars - 1):
        long_desired = signal[i]

        if long_desired and shares == 0:
            # --- ENTER at open of i+1 ---
            fill = open_px[i + 1] * (1.0 + slip)
            # cash must cover price + per-share commission + notional fee
            per_share_cost = fill * (1.0 + fee_rate) + commission_per_share
            if per_share_cost <= 0:
                equity_curve[i] = cash
                continue
            qty = int(cash // per_share_cost)
            if qty > 0:
                gross = qty * fill
                fee = gross * fee_rate + qty * commission_per_share
                cash -= (gross + fee)
                shares = qty
                entry_price = fill
                entry_ts = ts[i + 1]

        elif (not long_desired) and shares > 0:
            # --- EXIT at open of i+1 ---
            fill = open_px[i + 1] * (1.0 - slip)
            gross = shares * fill
            fee = gross * fee_rate + shares * commission_per_share
            cash += (gross - fee)
            trades.append({
                "entry_ts": entry_ts,
                "exit_ts": ts[i + 1],
                "shares": shares,
                "entry_px": round(float(entry_price), 4),
                "exit_px": round(float(fill), 4),
                "pnl": round(float((fill - entry_price) * shares - fee
                                   - (entry_price * shares * fee_rate + shares * commission_per_share)), 2),
                "return_pct": round(float((fill - entry_price) / entry_price * 100.0), 4),
                "bars_held": int(i + 1 - np.searchsorted(ts, entry_ts)),
            })
            shares = 0
            entry_price = 0.0
            entry_ts = None

        # Mark-to-market at close of day i
        equity_curve[i] = cash + shares * close_px[i]

    # Final mark-to-market at the last close (handles a still-open position).
    equity_curve[-1] = cash + shares * close_px[-1]
    if shares > 0:
        trades.append({
            "entry_ts": entry_ts,
            "exit_ts": ts[-1],
            "shares": shares,
            "entry_px": round(float(entry_price), 4),
            "exit_px": round(float(close_px[-1]), 4),
            "pnl": round(float((close_px[-1] - entry_price) * shares), 2),
            "return_pct": round(float((close_px[-1] - entry_price) / entry_price * 100.0), 4),
            "bars_held": int(n_bars - 1 - np.searchsorted(ts, entry_ts)),
            "note": "open at end of data (mark-to-market)",
        })

    equity_series = pd.Series(equity_curve, index=ts)
    return {
        "equity_curve": equity_series,
        "trades": pd.DataFrame(trades),
        "final_equity": float(cash + shares * close_px[-1]),
    }


# ---------------------------------------------------------------------------
# Performance metrics
# ---------------------------------------------------------------------------

def compute_metrics(equity_curve: pd.Series, trades: pd.DataFrame,
                    initial_capital: float) -> dict:
    """Total Return, CAGR, Sharpe, Max Drawdown, and trade-level stats."""
    final = float(equity_curve.iloc[-1])
    total_return = (final - initial_capital) / initial_capital

    daily_rets = equity_curve.pct_change().dropna()
    n_days = len(equity_curve)

    # CAGR: annualise over the number of trading days actually in the sample.
    years = n_days / 252.0
    if years > 0 and final > 0:
        cagr = (final / initial_capital) ** (1.0 / years) - 1.0
    else:
        cagr = float("nan")

    # Sharpe: mean daily return / std, annualised by sqrt(252).
    if daily_rets.std() > 0:
        sharpe = float(daily_rets.mean() / daily_rets.std() * np.sqrt(252))
    else:
        sharpe = 0.0

    # Max drawdown from the equity curve.
    running_max = equity_curve.cummax()
    drawdown = (equity_curve - running_max) / running_max
    max_dd = float(drawdown.min()) if len(drawdown) else 0.0

    metrics: dict = {
        "initial_capital": initial_capital,
        "final_equity": round(final, 2),
        "total_return_pct": round(total_return * 100.0, 2),
        "cagr_pct": round(cagr * 100.0, 2) if cagr == cagr else None,
        "sharpe": round(sharpe, 3),
        "max_drawdown_pct": round(max_dd * 100.0, 2),
        "trade_count": int(len(trades)),
    }

    if not trades.empty:
        pnl = trades["pnl"].astype(float)
        wins = pnl[pnl > 0]
        losses = pnl[pnl < 0]
        metrics.update({
            "win_rate_pct": round(float((pnl > 0).mean()) * 100.0, 2),
            "avg_trade_return_pct": round(float(trades["return_pct"].mean()), 3),
            "total_pnl_usd": round(float(pnl.sum()), 2),
            "profit_factor": round(float(wins.sum() / abs(losses.sum())), 2) if len(losses) else float("inf"),
            "avg_bars_held": round(float(trades["bars_held"].mean()), 1),
        })
    return metrics


# ---------------------------------------------------------------------------
# Buy-and-hold baseline
# ---------------------------------------------------------------------------

def buy_and_hold(df: pd.DataFrame, initial_capital: float,
                 slippage_bps: float, fee_rate: float, commission_per_share: float) -> dict:
    slip = slippage_bps / 10_000.0
    entry = df["open"].iloc[0] * (1.0 + slip)
    exit_px = df["close"].iloc[-1] * (1.0 - slip)
    per_share_cost = entry * (1.0 + fee_rate) + commission_per_share
    qty = int(initial_capital // per_share_cost)
    gross = qty * entry
    fee_in = gross * fee_rate + qty * commission_per_share
    final = qty * exit_px - (qty * exit_px * fee_rate + qty * commission_per_share)
    total_return = (final - (gross + fee_in)) / initial_capital
    years = len(df) / 252.0
    cagr = (final / initial_capital) ** (1.0 / years) - 1.0 if years > 0 and final > 0 else float("nan")
    return {
        "total_return_pct": round(total_return * 100.0, 2),
        "cagr_pct": round(cagr * 100.0, 2) if cagr == cagr else None,
        "final_equity": round(final, 2),
    }


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def render_report(cfg: dict, metrics: dict, bh: dict, trades: pd.DataFrame) -> str:
    lines: list[str] = []
    lines.append("=" * 62)
    lines.append(" SMA-SLOPE TREND-FOLLOWING BACKTEST  (long-only, daily)")
    lines.append("=" * 62)
    lines.append(f" Symbol        : {cfg['symbol']}   timeframe={cfg['timeframe']}   adjustment={cfg['adjustment']} feed={cfg['feed']}")
    lines.append(f" Window        : {cfg['start']}  ->  {cfg['end']}")
    lines.append(f" Params        : SMA(N={cfg['sma_period']})  slope(k={cfg['slope_lookback']})")
    lines.append(f" Costs         : slippage={cfg['slippage_bps']}bps  fee_rate={cfg['fee_rate']}  commission/share=${cfg['commission_per_share']}")
    lines.append("-" * 62)
    lines.append(" STRATEGY")
    lines.append(f"   Initial capital   : ${metrics['initial_capital']:,.2f}")
    lines.append(f"   Final equity      : ${metrics['final_equity']:,.2f}")
    lines.append(f"   Total return      : {metrics['total_return_pct']:+.2f}%")
    lines.append(f"   CAGR              : {metrics['cagr_pct']:+.2f}%")
    lines.append(f"   Sharpe ratio      : {metrics['sharpe']:.2f}")
    lines.append(f"   Max drawdown      : {metrics['max_drawdown_pct']:.2f}%")
    lines.append(f"   Trade count       : {metrics['trade_count']}")
    if "win_rate_pct" in metrics:
        lines.append(f"   Win rate          : {metrics['win_rate_pct']:.1f}%")
        lines.append(f"   Avg trade return  : {metrics['avg_trade_return_pct']:+.3f}%")
        lines.append(f"   Profit factor     : {metrics['profit_factor']}")
        lines.append(f"   Avg bars held     : {metrics['avg_bars_held']}")
    lines.append("-" * 62)
    lines.append(" BUY & HOLD BASELINE (same window & costs)")
    lines.append(f"   Final equity      : ${bh['final_equity']:,.2f}")
    lines.append(f"   Total return      : {bh['total_return_pct']:+.2f}%")
    lines.append(f"   CAGR              : {bh['cagr_pct']:+.2f}%")
    lines.append("-" * 62)
    if not trades.empty:
        lines.append(" TRADE LOG")
        for _, t in trades.iterrows():
            note_val = t.get("note")
            note = f"  ({note_val})" if pd.notna(note_val) else ""
            lines.append(
                f"   {pd.Timestamp(t['entry_ts']).date()} -> {pd.Timestamp(t['exit_ts']).date()} "
                f" {t['return_pct']:+.2f}%  bars={t['bars_held']}{note}"
            )
    lines.append("=" * 62)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="SMA-slope long-only daily backtest.")
    p.add_argument("--symbol", default=SYMBOL)
    p.add_argument("--start", default=START_DATE)
    p.add_argument("--end", default=END_DATE)
    p.add_argument("--sma", type=int, default=SMA_PERIOD, help="SMA period N")
    p.add_argument("--slope", type=int, default=SLOPE_LOOKBACK, help="Slope lookback k")
    p.add_argument("--capital", type=float, default=INITIAL_CAPITAL)
    p.add_argument("--slippage-bps", type=float, default=SLIPPAGE_BPS)
    p.add_argument("--commission-per-share", type=float, default=COMMISSION_PER_SHARE)
    p.add_argument("--fee-rate", type=float, default=FEE_RATE)
    p.add_argument("--json", action="store_true", help="Also dump metrics JSON to runtime/")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])

    df = load_bars(args.symbol, args.start, args.end)
    if len(df) < args.sma + args.slope + 10:
        print(f"ERROR: only {len(df)} bars — insufficient for SMA({args.sma}) + slope({args.slope}).", file=sys.stderr)
        return 1

    close = df["close"].to_numpy(dtype=float)
    signal = compute_signals(close, args.sma, args.slope)

    result = run_backtest(
        df, signal,
        initial_capital=args.capital,
        slippage_bps=args.slippage_bps,
        commission_per_share=args.commission_per_share,
        fee_rate=args.fee_rate,
    )
    metrics = compute_metrics(result["equity_curve"], result["trades"], args.capital)
    bh = buy_and_hold(df, args.capital, args.slippage_bps, args.fee_rate, args.commission_per_share)

    cfg = {
        "symbol": args.symbol, "timeframe": TIMEFRAME, "adjustment": ADJUSTMENT, "feed": FEED,
        "start": args.start, "end": args.end,
        "sma_period": args.sma, "slope_lookback": args.slope,
        "slippage_bps": args.slippage_bps, "fee_rate": args.fee_rate,
        "commission_per_share": args.commission_per_share,
    }

    print(render_report(cfg, metrics, bh, result["trades"]))

    if args.json:
        os.makedirs("runtime", exist_ok=True)
        out = {"config": cfg, "metrics": metrics, "buy_and_hold": bh}
        path = f"runtime/sma_slope_{args.symbol}_{args.sma}_{args.slope}.json"
        with open(path, "w") as f:
            json.dump(out, f, indent=2, default=str)
        print(f"\nJSON written to {path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
