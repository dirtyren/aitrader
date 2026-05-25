#!/usr/bin/env python3
"""Find which symbols work well with RSI(5) < 35 on 1H."""
import os, glob, json
import numpy as np
import pyarrow.parquet as pq

CACHE = os.path.expanduser("~/aitrader/runtime/bars_cache")

def rsi(prices, period=5):
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

results = []
for fpath in sorted(glob.glob(os.path.join(CACHE, "*.parquet"))):
    base = os.path.basename(fpath)
    sym = base.split("_")[0].replace("-", "/")
    table = pq.read_table(fpath, columns=["close", "ts"])
    df = table.to_pandas().rename(columns={"ts": "time"}).sort_values("time")
    closes_5min = df["close"].values.astype(np.float64)

    # Resample 5Min to 1H (factor 12)
    n_out = len(closes_5min) // 12
    if n_out < 30: continue
    closes_1h = closes_5min[11::12][:n_out]

    rsi_vals = rsi(closes_1h, 5)
    in_pos = False
    entry_px = 0.0
    returns = []
    wins = 0

    for i in range(5, len(closes_1h)):
        if np.isnan(rsi_vals[i]): continue
        if not in_pos:
            if rsi_vals[i] < 35:
                in_pos = True
                entry_px = closes_1h[i]
        else:
            if rsi_vals[i] > 50 or i == len(closes_1h) - 1:
                in_pos = False
                ret = (closes_1h[i] - entry_px) / entry_px
                returns.append(ret)
                if ret > 0: wins += 1

    if len(returns) < 5: continue
    returns = np.array(returns)
    wr = wins / len(returns)
    mean_r = np.mean(returns)
    std_r = np.std(returns)
    sharpe = mean_r / std_r * np.sqrt(252) if std_r > 0 and len(returns) > 1 else 0
    total = np.sum(returns)
    equity = 1000 * np.cumprod(1 + returns)
    dd = (equity - np.maximum.accumulate(equity)) / np.maximum.accumulate(equity)
    max_dd = float(abs(np.min(dd))) if len(dd) > 0 else 0
    score = wr * np.sqrt(len(returns)) * (1 - max_dd * 0.5)

    results.append({
        "symbol": sym, "signals": len(returns), "win_rate": round(wr, 3),
        "avg_return": round(float(mean_r), 5), "total_return": round(float(total), 4),
        "sharpe": round(sharpe, 2), "max_dd": round(max_dd, 4), "score": round(score, 2),
    })

results.sort(key=lambda x: x["score"], reverse=True)
print(json.dumps(results, indent=2))
