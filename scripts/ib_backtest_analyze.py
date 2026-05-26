#!/usr/bin/env python3
"""
IB Backtest — targeted analysis pass.
Loads the saved output and re-runs a focused query on positive-Sharpe combos only.
"""
import json, os, glob
from collections import defaultdict
import numpy as np
import pyarrow.parquet as pq
import pandas as pd

CACHE = os.path.expanduser("~/aitrader/runtime/bars_cache")
MIN_TRADES = 5
ATR_PERIOD = 14

SWEEP = {
    "5m":  {"ib_bars": [3, 6, 12, 24], "atr_ms": [0.75, 1.0, 1.25], "target_R": [1.5, 2.0, 2.5]},
    "15m": {"ib_bars": [2, 4, 8],      "atr_ms": [0.75, 1.0, 1.25], "target_R": [1.5, 2.0, 2.5]},
    "30m": {"ib_bars": [1, 2, 4],      "atr_ms": [0.75, 1.0, 1.25], "target_R": [1.5, 2.0, 2.5]},
    "1h":  {"ib_bars": [1, 2, 3],      "atr_ms": [0.75, 1.0, 1.25], "target_R": [1.5, 2.0, 2.5]},
    "4h":  {"ib_bars": [1, 2],         "atr_ms": [0.75, 1.0, 1.25], "target_R": [1.5, 2.0, 2.5]},
    "1D":  {"ib_bars": [1],            "atr_ms": [0.75, 1.0, 1.25], "target_R": [1.5, 2.0, 2.5]},
}


def compute_atr(high, low, close, period=14):
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
    ts = pd.Series(timestamps_utc)
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
            hour_min = t.hour * 60 + t.minute
            if t.weekday() < 5 and hour_min >= 570:
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
    trades = []
    for sess_start, sess_end in sessions:
        n_bars = sess_end - sess_start
        if n_bars < ib_bars + 2:
            continue
        ib_high = max(high[sess_start:sess_start + ib_bars])
        ib_low = min(low[sess_start:sess_start + ib_bars])
        for i in range(sess_start + ib_bars, sess_end):
            if np.isnan(atr[i]) or atr[i] <= 0:
                continue
            bar_atr = atr[i]
            prev_close = close[i-1]
            cur_close = close[i]
            if prev_close <= ib_high < cur_close:
                entry = cur_close
                stop = entry - atr_mult_stop * bar_atr
                target = entry + target_R * (entry - stop)
                exit_idx, exit_px, exit_reason = _find_exit(
                    high, low, close, i+1, min(sess_end, len(close)), 'long', stop, target)
                if exit_idx is not None:
                    trades.append({
                        "entry": float(entry), "exit": float(exit_px), "side": "long",
                        "stop": float(stop), "target": float(target),
                        "return": float((exit_px - entry) / entry),
                        "r_multiple": float((exit_px - entry) / (entry - stop)),
                        "bars_held": exit_idx - i, "exit_reason": exit_reason,
                    })
            elif prev_close >= ib_low > cur_close:
                entry = cur_close
                stop = entry + atr_mult_stop * bar_atr
                target = entry - target_R * (stop - entry)
                exit_idx, exit_px, exit_reason = _find_exit(
                    high, low, close, i+1, min(sess_end, len(close)), 'short', stop, target)
                if exit_idx is not None:
                    trades.append({
                        "entry": float(entry), "exit": float(exit_px), "side": "short",
                        "stop": float(stop), "target": float(target),
                        "return": float((entry - exit_px) / entry),
                        "r_multiple": float((entry - exit_px) / (stop - entry)),
                        "bars_held": exit_idx - i, "exit_reason": exit_reason,
                    })
    return trades


def _find_exit(high, low, close, start_idx, end_idx, side, stop, target):
    for j in range(start_idx, end_idx):
        if side == 'long':
            if low[j] <= stop: return j, stop, "stop"
            if high[j] >= target: return j, target, "target"
        else:
            if high[j] >= stop: return j, stop, "stop"
            if low[j] <= target: return j, target, "target"
    return end_idx - 1, close[end_idx - 1], "session_end"


def process_file(fpath):
    fname = os.path.basename(fpath)
    base = fname.replace('.parquet', '')
    tfs = sorted(SWEEP.keys(), key=len, reverse=True)
    symbol = tf_name = None
    for tf in tfs:
        marker = f'_{tf}_2025-01-01'
        if marker in base:
            symbol = base.split(marker)[0]
            tf_name = tf
            break
    if symbol is None or tf_name is None:
        return None
    is_crypto = '-USD' in symbol
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
    atr = compute_atr(high, low, close, ATR_PERIOD)
    if is_crypto:
        sessions = detect_crypto_sessions(df['ts'])
    else:
        sessions = detect_equity_sessions(df['ts'])
    if len(sessions) < 5:
        return None
    sweep = SWEEP[tf_name]
    results = []
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
                avg_r = np.mean(r_mults)
                std_r = np.std(r_mults) if len(r_mults) > 1 else 0.001
                sharpe = float(avg_r / std_r * np.sqrt(252)) if std_r > 0 else 0.0
                equity = 1000 * np.cumprod(1 + returns)
                roll_max = np.maximum.accumulate(equity)
                dd = (equity - roll_max) / roll_max
                max_dd = float(abs(np.min(dd))) if len(dd) > 0 else 0.0
                gross_win = np.sum(returns[returns > 0]) if wins > 0 else 0
                gross_loss = abs(np.sum(returns[returns <= 0])) if losses > 0 else 0
                pf = float(gross_win / gross_loss) if gross_loss > 0 else float('inf')
                results.append({
                    "symbol": symbol, "timeframe": tf_name,
                    "ib_bars": ib_bars, "atr_mult_stop": atr_ms, "target_R": tR,
                    "trades": len(trades), "win_rate": round(wr, 3), "avg_r": round(float(avg_r), 3),
                    "total_return": round(float(np.sum(returns)), 4),
                    "profit_factor": round(pf, 2), "sharpe": round(sharpe, 2),
                    "max_dd": round(max_dd, 4),
                })
    return results


def main():
    files = sorted(glob.glob(os.path.join(CACHE, "*_2025-01-01_2026-05-26.parquet")))
    print(f"Processing {len(files)} files...", file=__import__('sys').stderr)

    all_results = []
    for idx, fpath in enumerate(files):
        if (idx + 1) % 200 == 0:
            print(f"Progress: {idx+1}/{len(files)}", file=__import__('sys').stderr)
        r = process_file(fpath)
        if r:
            all_results.extend(r)

    print(f"\nTotal result sets: {len(all_results):,}", file=__import__('sys').stderr)

    # Save full dataset immediately
    out = os.path.expanduser("~/aitrader/runtime/ib_sweep_full.json")
    with open(out, 'w') as f:
        json.dump(all_results, f, indent=2)
    print(f"Full dataset saved to {out}", file=__import__('sys').stderr)

    # === ANALYSIS: Only positive-Sharpe, PF > 1 combos ===
    profitable = [r for r in all_results if r['sharpe'] > 0 and r['profit_factor'] > 1.0]
    print(f"Profitable combos (Sharpe>0, PF>1): {len(profitable):,}", file=__import__('sys').stderr)

    if not profitable:
        print("No profitable combos found.")
        return

    # 1. Best params by asset class × timeframe
    print("\n" + "="*100)
    print("1. BEST PARAMS — EQUITIES (by Sharpe)")
    print("="*100)
    eq_results = [r for r in profitable if '-USD' not in r['symbol']]
    eq_combos = defaultdict(list)
    for r in eq_results:
        key = f"IB({r['ib_bars']}b) stop={r['atr_mult_stop']}x R={r['target_R']}R on {r['timeframe']}"
        eq_combos[key].append(r)
    eq_scores = []
    for key, lst in eq_combos.items():
        avg_sharpe = np.mean([x['sharpe'] for x in lst])
        avg_wr = np.mean([x['win_rate'] for x in lst])
        avg_r = np.mean([x['avg_r'] for x in lst])
        avg_dd = np.mean([x['max_dd'] for x in lst])
        avg_pf = np.mean([x['profit_factor'] for x in lst])
        total_trades = sum(x['trades'] for x in lst)
        n_syms = len(set(x['symbol'] for x in lst))
        # Composite: weighted by sqrt(n_symbols) to prefer broad applicability
        comp = avg_sharpe * avg_pf * np.sqrt(n_syms) * (1 - avg_dd*3)
        eq_scores.append((comp, key, avg_sharpe, avg_wr, avg_r, avg_dd, avg_pf, total_trades, n_syms))
    eq_scores.sort(key=lambda x: x[0], reverse=True)
    print(f"{'Rank':<5} {'Combo':<50} {'Sharpe':<8} {'WR':<6} {'AvgR':<6} {'DD':<6} {'PF':<6} {'Trades':<8} {'Syms':<5}")
    print("-"*100)
    for i, (_, key, sh, wr, ar, dd, pf, tr, ns) in enumerate(eq_scores[:15], 1):
        print(f"{i:<5} {key:<50} {sh:<8.2f} {wr:<6.1%} {ar:<6.3f} {dd:<6.2%} {pf:<6.2f} {tr:<8,} {ns:<5}")

    # 2. Best params by timeframe
    print("\n" + "="*100)
    print("2. BEST PARAMS BY TIMEFRAME — ALL ASSETS")
    print("="*100)
    for tf in ['5m', '15m', '30m', '1h', '4h', '1D']:
        tf_results = [r for r in profitable if r['timeframe'] == tf]
        if not tf_results:
            continue
        tf_combos = defaultdict(list)
        for r in tf_results:
            key = f"IB({r['ib_bars']}b) stop={r['atr_mult_stop']}x R={r['target_R']}R"
            tf_combos[key].append(r)
        entries = []
        for key, lst in tf_combos.items():
            avg_sh = np.mean([x['sharpe'] for x in lst])
            avg_wr = np.mean([x['win_rate'] for x in lst])
            avg_r = np.mean([x['avg_r'] for x in lst])
            avg_dd = np.mean([x['max_dd'] for x in lst])
            avg_pf = np.mean([x['profit_factor'] for x in lst])
            total_tr = sum(x['trades'] for x in lst)
            n_syms = len(set(x['symbol'] for x in lst))
            entries.append((avg_sh, key, avg_wr, avg_r, avg_dd, avg_pf, total_tr, n_syms))
        entries.sort(key=lambda x: x[0], reverse=True)
        print(f"\n{tf}:")
        print(f"  {'Combo':<40} {'Sharpe':<8} {'WR':<6} {'AvgR':<6} {'DD':<6} {'PF':<6} {'Trades':<10} {'Syms':<5}")
        print(f"  {'-'*81}")
        for sh, key, wr, ar, dd, pf, tr, ns in entries[:5]:
            print(f"  {key:<40} {sh:<8.2f} {wr:<6.1%} {ar:<6.3f} {dd:<6.2%} {pf:<6.2f} {tr:<10,} {ns:<5}")

    # 3. Best params for crypto
    print("\n" + "="*100)
    print("3. BEST PARAMS — CRYPTO (by Sharpe)")
    print("="*100)
    cry_results = [r for r in profitable if '-USD' in r['symbol']]
    cry_combos = defaultdict(list)
    for r in cry_results:
        key = f"IB({r['ib_bars']}b) stop={r['atr_mult_stop']}x R={r['target_R']}R on {r['timeframe']}"
        cry_combos[key].append(r)
    cry_scores = []
    for key, lst in cry_combos.items():
        avg_sh = np.mean([x['sharpe'] for x in lst])
        avg_wr = np.mean([x['win_rate'] for x in lst])
        avg_r = np.mean([x['avg_r'] for x in lst])
        avg_dd = np.mean([x['max_dd'] for x in lst])
        avg_pf = np.mean([x['profit_factor'] for x in lst])
        total_tr = sum(x['trades'] for x in lst)
        n_syms = len(set(x['symbol'] for x in lst))
        cry_scores.append((avg_sh, key, avg_wr, avg_r, avg_dd, avg_pf, total_tr, n_syms))
    cry_scores.sort(key=lambda x: x[0], reverse=True)
    print(f"{'Rank':<5} {'Combo':<50} {'Sharpe':<8} {'WR':<6} {'AvgR':<6} {'DD':<6} {'PF':<6} {'Trades':<8} {'Syms':<5}")
    print("-"*100)
    for i, (sh, key, wr, ar, dd, pf, tr, ns) in enumerate(cry_scores[:15], 1):
        print(f"{i:<5} {key:<50} {sh:<8.2f} {wr:<6.1%} {ar:<6.3f} {dd:<6.2%} {pf:<6.2f} {tr:<8,} {ns:<5}")

    # 4. Best symbols overall
    print("\n" + "="*100)
    print("4. TOP 20 SYMBOLS — BEST SINGLE PARAM SET (by Sharpe)")
    print("="*100)
    best_per_sym = {}
    for r in profitable:
        sym = r['symbol']
        if sym not in best_per_sym or r['sharpe'] > best_per_sym[sym]['sharpe']:
            best_per_sym[sym] = r
    sym_by_sharpe = sorted(best_per_sym.values(), key=lambda x: x['sharpe'], reverse=True)
    print(f"{'Rank':<5} {'Symbol':<10} {'Timeframe':<10} {'IB(b)':<6} {'Stop':<6} {'R':<5} {'Sharpe':<8} {'WR':<6} {'AvgR':<6} {'DD':<6} {'PF':<5} {'Trades':<5}")
    print("-"*90)
    for i, r in enumerate(sym_by_sharpe[:20], 1):
        print(f"{i:<5} {r['symbol']:<10} {r['timeframe']:<10} {r['ib_bars']:<6} {r['atr_mult_stop']:<6} {r['target_R']:<5} "
              f"{r['sharpe']:<8.2f} {r['win_rate']:<6.1%} {r['avg_r']:<6.3f} {r['max_dd']:<6.2%} "
              f"{r['profit_factor']:<5.2f} {r['trades']:<5}")

    # 5. Win rate analysis
    print("\n" + "="*100)
    print("5. WIN RATE DISTRIBUTION — HIGH WR COMBOS")
    print("="*100)
    high_wr = [r for r in profitable if r['win_rate'] > 0.40 and r['trades'] >= 10]
    high_wr.sort(key=lambda x: x['win_rate'], reverse=True)
    print(f"{'Rank':<5} {'Symbol':<10} {'TF':<6} {'Params':<35} {'WR':<6} {'Sharpe':<8} {'AvgR':<6} {'DD':<6} {'PF':<5} {'Trades':<5}")
    print("-"*90)
    for i, r in enumerate(high_wr[:20], 1):
        p = f"IB({r['ib_bars']}b) stop={r['atr_mult_stop']}x R={r['target_R']}R"
        print(f"{i:<5} {r['symbol']:<10} {r['timeframe']:<6} {p:<35} {r['win_rate']:<6.1%} {r['sharpe']:<8.2f} "
              f"{r['avg_r']:<6.3f} {r['max_dd']:<6.2%} {r['profit_factor']:<5.2f} {r['trades']:<5}")

    # 6. Summary stats
    print("\n" + "="*100)
    print("6. SUMMARY STATS")
    print("="*100)
    print(f"Total profitable combos: {len(profitable):,}")
    print(f"  - Equity: {len(eq_results):,}")
    print(f"  - Crypto: {len(cry_results):,}")
    print(f"Symbols with ≥1 profitable combo: {len(best_per_sym)}")
    eq_syms = len(set(r['symbol'] for r in eq_results))
    cry_syms = len(set(r['symbol'] for r in cry_results))
    print(f"  - Equity symbols: {eq_syms}")
    print(f"  - Crypto symbols: {cry_syms}")
    
    # Avg stats of top 10 equity combos
    top10_eq = [x for x in eq_scores[:10]]
    print(f"\nTop 10 Equity Combos — Avg: Sharpe={np.mean([x[2] for x in top10_eq]):.2f} "
          f"WR={np.mean([x[3] for x in top10_eq]):.1%} AvgR={np.mean([x[4] for x in top10_eq]):.3f} "
          f"DD={np.mean([x[5] for x in top10_eq]):.2%} PF={np.mean([x[6] for x in top10_eq]):.2f}")
    
    top10_cry = [x for x in cry_scores[:10]]
    print(f"Top 10 Crypto Combos — Avg: Sharpe={np.mean([x[1] for x in top10_cry]):.2f} "
          f"WR={np.mean([x[3] for x in top10_cry]):.1%} AvgR={np.mean([x[4] for x in top10_cry]):.3f} "
          f"DD={np.mean([x[5] for x in top10_cry]):.2%} PF={np.mean([x[6] for x in top10_cry]):.2f}")

    # Save full dataset
    out = os.path.expanduser("~/aitrader/runtime/ib_sweep_full.json")
    with open(out, 'w') as f:
        json.dump(all_results, f, indent=2)
    print(f"\nFull dataset saved to {out}")


if __name__ == "__main__":
    main()