#!/usr/bin/env python3
"""
RSI Reversion Backtest v2 — minimum 10 signals, better scoring.
Tests: symbols × timeframes × RSI period (2-5) × threshold (10-40 step 5)
"""
import os, glob, json
from collections import defaultdict

import numpy as np
import pyarrow.parquet as pq

CACHE = os.path.expanduser("~/aitrader/runtime/bars_cache")
TIMEFRAMES = {"5Min": 1, "15Min": 3, "30Min": 6, "1H": 12, "4H": 48}
PERIODS = range(2, 6)
THRESHOLDS = range(10, 45, 5)
MIN_TRADES = 10


def rsi(prices, period):
    deltas = np.diff(prices)
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    rsi_vals = np.full_like(prices, np.nan)
    avg_gain = np.mean(gains[:period])
    avg_loss = np.mean(losses[:period]) if np.mean(losses[:period]) > 0 else 0.001
    rsi_vals[period] = 100 - 100 / (1 + avg_gain / avg_loss)
    for i in range(period + 1, len(prices)):
        avg_gain = (avg_gain * (period - 1) + gains[i - 1]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i - 1]) / period
        if avg_loss == 0: avg_loss = 0.001
        rsi_vals[i] = 100 - 100 / (1 + avg_gain / avg_loss)
    return rsi_vals


def resample_prices(closes, factor):
    if factor <= 1: return closes
    n_out = len(closes) // factor
    if n_out < 20: return None
    return closes[factor - 1::factor][:n_out]


def backtest_rsi(prices, period, entry_thresh, exit_thresh=50):
    rsi_vals = rsi(prices, period)
    n = len(prices)
    in_pos = False
    entry_px = 0.0
    entry_idx = 0
    returns = []
    wins = 0

    for i in range(period, n):
        if np.isnan(rsi_vals[i]): continue
        if not in_pos:
            if rsi_vals[i] < entry_thresh:
                in_pos = True
                entry_px = prices[i]
                entry_idx = i
        else:
            if rsi_vals[i] > exit_thresh or i == n - 1:
                in_pos = False
                ret = (prices[i] - entry_px) / entry_px
                returns.append(ret)
                if ret > 0: wins += 1

    if len(returns) < MIN_TRADES:
        return None

    returns = np.array(returns)
    sharpe_factor = {"5Min": 252*78, "15Min": 252*26, "30Min": 252*13, "1H": 252*6.5, "4H": 252*1.625}
    # Use annualized sharpe
    daily_rets = returns  # each trade is ~1 bar on its timeframe
    mean_r = np.mean(returns)
    std_r = np.std(returns)
    sharpe = float(mean_r / std_r * np.sqrt(252)) if std_r > 0 and len(returns) > 1 else 0.0

    # Equity curve for max drawdown
    equity = 1000 * np.cumprod(1 + returns)
    rolling_max = np.maximum.accumulate(equity)
    dd = (equity - rolling_max) / rolling_max
    max_dd = float(abs(np.min(dd)))

    win_rate = wins / len(returns)

    # Composite score: favors more signals, higher WR, higher returns, lower DD
    score = (win_rate * 10 + np.mean(returns) * 100) * np.sqrt(len(returns)) * (1 - max_dd)

    return {
        "signals": len(returns),
        "win_rate": round(win_rate, 3),
        "avg_return": round(float(np.mean(returns)), 5),
        "total_return": round(float(np.sum(returns)), 4),
        "max_dd": round(max_dd, 4),
        "sharpe": round(sharpe, 3),
        "score": round(float(score), 2),
    }


def main():
    files = sorted(glob.glob(os.path.join(CACHE, "*.parquet")))
    results = []

    for fpath in files:
        base = os.path.basename(fpath)
        sym = base.split("_")[0].replace("-", "/")
        table = pq.read_table(fpath, columns=["close", "ts"])
        df = table.to_pandas().rename(columns={"ts": "time"}).sort_values("time")
        closes_5min = df["close"].values.astype(np.float64)
        if len(closes_5min) < 100: continue

        for tf_name, factor in TIMEFRAMES.items():
            rcloses = resample_prices(closes_5min, factor)
            if rcloses is None: continue

            for period in PERIODS:
                if period >= len(rcloses) - 5: continue
                for thresh in THRESHOLDS:
                    m = backtest_rsi(rcloses, period, thresh)
                    if m is None: continue
                    results.append({
                        "symbol": sym, "timeframe": tf_name,
                        "period": period, "entry": thresh, **m,
                    })

    if not results:
        print(json.dumps({"total": 0}))
        return

    results.sort(key=lambda x: x["score"], reverse=True)

    # Best per symbol (min 10 signals)
    best_per_sym = {}
    for r in results:
        if r["signals"] < MIN_TRADES: continue
        sym = r["symbol"]
        if sym not in best_per_sym or r["score"] > best_per_sym[sym]["score"]:
            best_per_sym[sym] = r

    # Best per timeframe (min 10 signals)
    best_per_tf = defaultdict(list)
    for r in results:
        if r["signals"] < MIN_TRADES: continue
        best_per_tf[r["timeframe"]].append(r)
    best_tf = {}
    for tf, lst in best_per_tf.items():
        lst.sort(key=lambda x: x["score"], reverse=True)
        best_tf[tf] = lst[:5]

    # Sweep summary: avg metrics per period × threshold combo across all symbols
    sweep_summary = defaultdict(list)
    for r in results:
        if r["signals"] < MIN_TRADES: continue
        key = f"RSI({r['period']}) < {r['entry']} on {r['timeframe']}"
        sweep_summary[key].append(r)

    top_sweep = []
    for key, lst in sweep_summary.items():
        avg_wr = np.mean([x["win_rate"] for x in lst])
        avg_r = np.mean([x["avg_return"] for x in lst])
        avg_sharpe = np.mean([x["sharpe"] for x in lst])
        total_signals = sum(x["signals"] for x in lst)
        unique_symbols = len(set(x["symbol"] for x in lst))
        top_sweep.append({
            "combo": key,
            "total_signals": total_signals,
            "unique_symbols": unique_symbols,
            "avg_win_rate": round(avg_wr, 3),
            "avg_return": round(avg_r, 5),
            "avg_sharpe": round(avg_sharpe, 2),
        })
    top_sweep.sort(key=lambda x: x["avg_sharpe"], reverse=True)

    # Best overall (top 10 with >=10 signals)
    top10 = [r for r in results if r["signals"] >= MIN_TRADES][:10]

    output = {
        "total_combos_tested": len(results),
        "combos_with_10plus_signals": sum(1 for r in results if r["signals"] >= MIN_TRADES),
        "best_per_symbol": list(best_per_sym.values()),
        "best_per_timeframe_top5": dict(best_tf),
        "top_10_overall": top10,
        "sweep_summary_top15": top_sweep[:15],
    }

    print(json.dumps(output, indent=2, default=str))


if __name__ == "__main__":
    main()