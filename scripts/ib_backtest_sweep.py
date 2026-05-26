#!/usr/bin/env python3
"""
Initial Balance Breakout Backtest Sweep v1.
Tests: 510 symbols (500 stocks + 10 crypto) × 6 timeframes (5m/15m/30m/1h/4h/1D)
        × ib_bars × atr_mult_stop × target_R

IB Logic (Trend Breakout):
  - Per session: establish IB range from first N bars (IB_high, IB_low)
  - On each subsequent bar: prev_close <= IB_high < close → long entry
                              prev_close >= IB_low > close → short entry
  - Entry at bar close, stop = entry ± atr_mult_stop * ATR(14)
  - Target = entry ± target_R * (entry - stop)
  - Exit when stop/target hit in subsequent bars

Sessions:
  - Equities (no "-USD"): 09:30-16:00 ET trading days
  - Crypto ("-USD"): 00:00-23:59 UTC daily
"""
import os, glob, json, sys
from collections import defaultdict

import numpy as np
import pyarrow.parquet as pq
import pandas as pd

CACHE = os.path.expanduser("~/aitrader/runtime/bars_cache")
MIN_TRADES = 5
ATR_PERIOD = 14

# Parameter sweeps per timeframe
SWEEP = {
    "5m":  {"ib_bars": [3, 6, 12, 24], "atr_ms": [0.75, 1.0, 1.25], "target_R": [1.5, 2.0, 2.5]},
    "15m": {"ib_bars": [2, 4, 8],      "atr_ms": [0.75, 1.0, 1.25], "target_R": [1.5, 2.0, 2.5]},
    "30m": {"ib_bars": [1, 2, 4],      "atr_ms": [0.75, 1.0, 1.25], "target_R": [1.5, 2.0, 2.5]},
    "1h":  {"ib_bars": [1, 2, 3],      "atr_ms": [0.75, 1.0, 1.25], "target_R": [1.5, 2.0, 2.5]},
    "4h":  {"ib_bars": [1, 2],         "atr_ms": [0.75, 1.0, 1.25], "target_R": [1.5, 2.0, 2.5]},
    "1D":  {"ib_bars": [1],            "atr_ms": [0.75, 1.0, 1.25], "target_R": [1.5, 2.0, 2.5]},
}

# Timeframe in minutes for display
TF_MINUTES = {"5m": 5, "15m": 15, "30m": 30, "1h": 60, "4h": 240, "1D": 1440}


def compute_atr(high, low, close, period=14):
    """Compute ATR(period) as simple moving average of True Range."""
    n = len(high)
    if n < period + 1:
        return np.full(n, np.nan)
    tr = np.zeros(n)
    tr[0] = high[0] - low[0]
    for i in range(1, n):
        tr[i] = max(high[i] - low[i], abs(high[i] - close[i-1]), abs(low[i] - close[i-1]))
    atr = np.full(n, np.nan)
    atr[period] = np.mean(tr[1:period+1])
    for i in range(period+1, n):
        atr[i] = (atr[i-1] * (period - 1) + tr[i]) / period
    return atr


def detect_equity_sessions(timestamps_utc):
    """Group equity bars into trading-day sessions (09:30-16:00 ET).
    Returns list of (start_idx, end_idx) for each session."""
    ts = pd.Series(timestamps_utc)
    # Convert to ET (UTC-4 EDT, UTC-5 EST — we use flexible)
    et = ts.dt.tz_convert('America/New_York')
    dates = et.dt.date

    sessions = []
    current_date = None
    start_idx = None

    for i, (d, t) in enumerate(zip(dates, et)):
        if d != current_date:
            if start_idx is not None and i - start_idx > 1:
                sessions.append((start_idx, i))
            current_date = d
            # Only start sessions on weekdays
            hour_min = t.hour * 60 + t.minute
            if t.weekday() < 5 and hour_min >= 570:  # 09:30 ET = 570 min
                start_idx = i
            else:
                start_idx = None
        else:
            hour_min = t.hour * 60 + t.minute
            if start_idx is None and t.weekday() < 5 and hour_min >= 570:
                start_idx = i

    if start_idx is not None:
        sessions.append((start_idx, len(et)))

    return sessions


def detect_crypto_sessions(timestamps_utc):
    """Group crypto bars into UTC-day sessions (00:00-23:59 UTC)."""
    ts = pd.Series(timestamps_utc)
    dates = ts.dt.date

    sessions = []
    start_idx = 0
    for i in range(1, len(dates)):
        if dates[i] != dates[start_idx]:
            sessions.append((start_idx, i))
            start_idx = i
    if start_idx < len(dates) - 1:
        sessions.append((start_idx, len(dates)))
    return sessions


def run_ib_backtest(high, low, close, atr, sessions, ib_bars, atr_mult_stop, target_R):
    """Run IB breakout backtest. Returns list of trade dicts."""
    trades = []

    for sess_start, sess_end in sessions:
        n_bars = sess_end - sess_start
        if n_bars < ib_bars + 2:  # Need IB + 1 bar for signal + exit
            continue

        # Compute IB range
        ib_high = max(high[sess_start:sess_start + ib_bars])
        ib_low = min(low[sess_start:sess_start + ib_bars])

        # Scan for breakout signals after IB formation
        for i in range(sess_start + ib_bars, sess_end):
            if np.isnan(atr[i]) or atr[i] <= 0:
                continue

            bar_atr = atr[i]
            prev_close = close[i-1]
            cur_close = close[i]

            # Long: prev_close <= IB_high < cur_close
            if prev_close <= ib_high < cur_close:
                entry = cur_close
                stop = entry - atr_mult_stop * bar_atr
                target = entry + target_R * (entry - stop)

                # Find exit in subsequent bars
                exit_idx, exit_px, exit_reason = _find_exit(
                    high, low, close, i+1, min(sess_end, len(close)),
                    side='long', stop=stop, target=target, entry=entry
                )

                # Track result
                if exit_idx is not None:
                    ret = (exit_px - entry) / entry
                    r_multiple = (exit_px - entry) / (entry - stop)
                    trades.append({
                        "side": "long", "entry": float(entry), "exit": float(exit_px),
                        "stop": float(stop), "target": float(target),
                        "return": float(ret), "r_multiple": float(r_multiple),
                        "bars_held": exit_idx - i, "exit_reason": exit_reason,
                        "ib_bars": ib_bars, "atr_mult_stop": atr_mult_stop, "target_R": target_R,
                    })

            # Short: prev_close >= IB_low > cur_close
            elif prev_close >= ib_low > cur_close:
                entry = cur_close
                stop = entry + atr_mult_stop * bar_atr
                target = entry - target_R * (stop - entry)

                exit_idx, exit_px, exit_reason = _find_exit(
                    high, low, close, i+1, min(sess_end, len(close)),
                    side='short', stop=stop, target=target, entry=entry
                )

                if exit_idx is not None:
                    ret = (entry - exit_px) / entry
                    r_multiple = (entry - exit_px) / (stop - entry)
                    trades.append({
                        "side": "short", "entry": float(entry), "exit": float(exit_px),
                        "stop": float(stop), "target": float(target),
                        "return": float(ret), "r_multiple": float(r_multiple),
                        "bars_held": exit_idx - i, "exit_reason": exit_reason,
                        "ib_bars": ib_bars, "atr_mult_stop": atr_mult_stop, "target_R": target_R,
                    })

    return trades


def _find_exit(high, low, close, start_idx, end_idx, side, stop, target, entry):
    """Find exit bar: whichever of stop/target is hit first."""
    for j in range(start_idx, end_idx):
        if side == 'long':
            # Hit stop?
            if low[j] <= stop:
                return j, stop, "stop"
            # Hit target?
            if high[j] >= target:
                return j, target, "target"
            # Session end - exit at close
        else:  # short
            if high[j] >= stop:
                return j, stop, "stop"
            if low[j] <= target:
                return j, target, "target"

    # Exit at session end close
    return end_idx - 1, close[end_idx - 1], "session_end"


def process_file(fpath):
    """Process one parquet file across all parameter combos for its timeframe."""
    fname = os.path.basename(fpath)

    # Parse symbol and timeframe from filename
    base = fname.replace('.parquet', '')
    tfs = sorted(SWEEP.keys(), key=len, reverse=True)

    symbol = None
    tf_name = None
    for tf in tfs:
        marker = f'_{tf}_2025-01-01'
        if marker in base:
            symbol = base.split(marker)[0]
            tf_name = tf
            break

    if symbol is None or tf_name is None:
        return None

    is_crypto = '-USD' in symbol

    # Load data
    try:
        df = pq.read_table(fpath, columns=['ts', 'open', 'high', 'low', 'close', 'volume']).to_pandas()
    except Exception:
        return None

    if len(df) < 50:
        return None

    df = df.sort_values('ts').reset_index(drop=True)

    high = df['high'].values.astype(np.float64)
    low = df['low'].values.astype(np.float64)
    close = df['close'].values.astype(np.float64)

    # ATR
    atr = compute_atr(high, low, close, ATR_PERIOD)

    # Sessions
    if is_crypto:
        sessions = detect_crypto_sessions(df['ts'])
    else:
        sessions = detect_equity_sessions(df['ts'])

    if len(sessions) < 5:
        return None

    # Run all parameter combos for this timeframe
    sweep = SWEEP[tf_name]
    all_results = []

    for ib_bars in sweep['ib_bars']:
        for atr_ms in sweep['atr_ms']:
            for tR in sweep['target_R']:
                trades = run_ib_backtest(high, low, close, atr, sessions, ib_bars, atr_ms, tR)
                if len(trades) < MIN_TRADES:
                    continue

                returns = np.array([t['return'] for t in trades])
                r_mults = np.array([t['r_multiple'] for t in trades])
                wins = np.sum(returns > 0)
                losses = np.sum(returns <= 0)
                wr = wins / len(returns)

                # Annualized Sharpe
                avg_r = np.mean(r_mults)
                std_r = np.std(r_mults) if len(r_mults) > 1 else 0.001
                # For intraday: scale by ~252 trading days / avg bars per trade
                # Conservative annualization
                sharpe = float(avg_r / std_r * np.sqrt(252)) if std_r > 0 else 0.0

                # Equity curve for max DD
                equity = 1000 * np.cumprod(1 + returns)
                roll_max = np.maximum.accumulate(equity)
                dd = (equity - roll_max) / roll_max
                max_dd = float(abs(np.min(dd))) if len(dd) > 0 else 0.0

                # Profit factor
                gross_win = np.sum(returns[returns > 0]) if wins > 0 else 0
                gross_loss = abs(np.sum(returns[returns <= 0])) if losses > 0 else 0
                profit_factor = float(gross_win / gross_loss) if gross_loss > 0 else float('inf')

                # Composite score: favors WR, Sharpe, low DD, sufficient signals
                score = (wr * 10 + avg_r * 20) * np.sqrt(len(trades)) * (1 - max_dd * 2) * min(sharpe / 0.5, 1.5)

                all_results.append({
                    "symbol": symbol, "timeframe": tf_name,
                    "ib_bars": ib_bars, "atr_mult_stop": atr_ms, "target_R": tR,
                    "trades": len(trades),
                    "win_rate": round(wr, 3),
                    "avg_r": round(float(avg_r), 3),
                    "total_return": round(float(np.sum(returns)), 4),
                    "profit_factor": round(profit_factor, 2),
                    "sharpe": round(sharpe, 2),
                    "max_dd": round(max_dd, 4),
                    "score": round(float(score), 2),
                })

    if not all_results:
        return None

    return all_results


def main():
    files = sorted(glob.glob(os.path.join(CACHE, "*_2025-01-01_2026-05-26.parquet")))

    if len(files) == 0:
        print("ERROR: No parquet files found matching pattern", file=sys.stderr)
        sys.exit(1)

    print(f"Found {len(files)} parquet files. Processing...", file=sys.stderr)

    all_results = []
    skipped = 0

    for idx, fpath in enumerate(files):
        if (idx + 1) % 100 == 0:
            print(f"Progress: {idx+1}/{len(files)} files processed ({len(all_results)} result sets)...", file=sys.stderr)

        result = process_file(fpath)
        if result is None:
            skipped += 1
            continue
        all_results.extend(result)

    print(f"\nProcessed {len(files)} files, {skipped} skipped, {len(all_results)} parameter combos tested", file=sys.stderr)

    if not all_results:
        print(json.dumps({"total": 0}))
        return

    # Sort by score descending
    all_results.sort(key=lambda x: x["score"], reverse=True)

    # Best per symbol
    best_per_sym = {}
    for r in all_results:
        sym = r["symbol"]
        if sym not in best_per_sym or r["score"] > best_per_sym[sym]["score"]:
            best_per_sym[sym] = r

    # Best per timeframe
    best_per_tf = defaultdict(list)
    for r in all_results:
        best_per_tf[r["timeframe"]].append(r)
    best_tf = {}
    for tf, lst in best_per_tf.items():
        lst.sort(key=lambda x: x["score"], reverse=True)
        best_tf[tf] = lst[:5]

    # Sweep summary: avg metrics per combo across all symbols
    sweep_summary = defaultdict(list)
    for r in all_results:
        key = f"IB({r['ib_bars']}b) stop={r['atr_mult_stop']}x R={r['target_R']}R on {r['timeframe']}"
        sweep_summary[key].append(r)

    top_sweep = []
    for key, lst in sweep_summary.items():
        avg_wr = np.mean([x["win_rate"] for x in lst])
        avg_r = np.mean([x["avg_r"] for x in lst])
        avg_sharpe = np.mean([x["sharpe"] for x in lst])
        avg_dd = np.mean([x["max_dd"] for x in lst])
        avg_pf = np.mean([x["profit_factor"] for x in lst])
        total_signals = sum(x["trades"] for x in lst)
        unique_symbols = len(set(x["symbol"] for x in lst))
        top_sweep.append({
            "combo": key,
            "total_signals": total_signals,
            "unique_symbols": unique_symbols,
            "avg_win_rate": round(avg_wr, 3),
            "avg_r": round(avg_r, 3),
            "avg_sharpe": round(avg_sharpe, 2),
            "avg_dd": round(avg_dd, 4),
            "avg_profit_factor": round(avg_pf, 2),
        })
    top_sweep.sort(key=lambda x: x["avg_sharpe"], reverse=True)

    # Top 20 overall
    top20 = [r for r in all_results if r["trades"] >= MIN_TRADES][:20]

    # Best per symbol-type (equity vs crypto)
    equity_results = [r for r in all_results if '-USD' not in r['symbol']]
    crypto_results = [r for r in all_results if '-USD' in r['symbol']]

    best_equity_combo = defaultdict(list)
    for r in equity_results:
        key = f"IB({r['ib_bars']}b) stop={r['atr_mult_stop']}x R={r['target_R']}R on {r['timeframe']}"
        best_equity_combo[key].append(r)

    best_crypto_combo = defaultdict(list)
    for r in crypto_results:
        key = f"IB({r['ib_bars']}b) stop={r['atr_mult_stop']}x R={r['target_R']}R on {r['timeframe']}"
        best_crypto_combo[key].append(r)

    def summarize_combo_group(group):
        """Sort and return top entries for a grouped combo dict."""
        entries = []
        for key, lst in group.items():
            avg_wr = np.mean([x["win_rate"] for x in lst])
            avg_r = np.mean([x["avg_r"] for x in lst])
            avg_sharpe = np.mean([x["sharpe"] for x in lst])
            avg_dd = np.mean([x["max_dd"] for x in lst])
            total_signals = sum(x["trades"] for x in lst)
            unique = len(set(x["symbol"] for x in lst))
            entries.append({
                "combo": key, "total_signals": total_signals,
                "symbols": unique, "avg_win_rate": round(avg_wr, 3),
                "avg_r": round(avg_r, 3), "avg_sharpe": round(avg_sharpe, 2),
                "avg_dd": round(avg_dd, 4),
            })
        entries.sort(key=lambda x: x["avg_sharpe"], reverse=True)
        return entries[:10]

    output = {
        "total_combos_tested": len(all_results),
        "symbols_with_trades": len(best_per_sym),
        "best_equity_params": summarize_combo_group(best_equity_combo),
        "best_crypto_params": summarize_combo_group(best_crypto_combo),
        "best_per_timeframe_top5": {k: v for k, v in best_tf.items()},
        "top_20_overall": top20,
        "sweep_summary_top20": top_sweep[:20],
    }

    # Save to file
    outpath = os.path.expanduser("~/aitrader/runtime/ib_backtest_results.json")
    with open(outpath, 'w') as f:
        json.dump(output, f, indent=2, default=str)
    print(f"\nResults saved to {outpath}", file=sys.stderr)
    print(json.dumps(output, indent=2, default=str))


if __name__ == "__main__":
    main()