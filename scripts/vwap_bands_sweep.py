"""
VWAP Deviation Bands — Multi-Timeframe Parameter Sweep
=======================================================
Sweeps sigma, atr_mult_stop, target_R across N timeframes and 500+ symbols.
Reports the best parameter combos per timeframe ranked by win rate / expectancy.

Usage:
    python scripts/vwap_bands_sweep.py [--timeframes 5Min,15Min,30Min,1H,4H,1D]
                                       [--start 2025-01-01]
                                       [--end 2026-05-26]
                                       [--equity-symbols 500]
                                       [--crypto-symbols 10]
                                       [--max-workers 4]
                                       [--cache-only]   # only fetch bars, don't backtest
"""

import argparse
import math
import os
import sys
import time
import json
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

# ── ensure we can import from the aitrader project ──────────────────────
PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT)
os.chdir(PROJECT)

from broker.alpaca_client import AlpacaClient
from broker.alpaca_data import AlpacaData
from broker.symbol import normalize_for_api
from dotenv import load_dotenv

load_dotenv(os.path.join(PROJECT, "config", ".env"))

# ── Parameter grid ─────────────────────────────────────────────────────
SIGMA_VALUES = [1.5, 2.0, 2.5, 3.0]
ATR_MULT_STOP_VALUES = [0.5, 0.75, 1.0, 1.25, 1.5]
TARGET_R_VALUES = [1.5, 2.0, 2.5, 3.0]

TIMEFRAMES = ["5Min", "15Min", "30Min", "1H", "4H", "1D"]

# Top 10 crypto by market cap (USD pairs supported by Alpaca)
CRYPTO_SYMBOLS = [
    "BTC/USD", "ETH/USD", "XRP/USD", "SOL/USD", "DOGE/USD",
    "ADA/USD", "AVAX/USD", "LINK/USD", "DOT/USD", "PEPE/USD",
]

# ═══════════════════════════════════════════════════════════════════════
# S&P 500 stock list (hardcoded to avoid another fetch)
# ═══════════════════════════════════════════════════════════════════════
SP500_SYMBOLS = [
    "MMM","AOS","ABT","ABBV","ACN","ADBE","AMD","AES","AFL","A",
    "APD","ABNB","AKAM","ALB","ARE","ALGN","ALLE","LNT","ALL","GOOGL",
    "GOOG","MO","AMZN","AMCR","AEE","AEP","AXP","AIG","AMT","AWK",
    "AMP","AME","AMGN","APH","ADI","AON","APA","APO","AAPL","AMAT",
    "APP","APTV","ACGL","ADM","ARES","ANET","AJG","AIZ","T","ATO",
    "ADSK","ADP","AZO","AVB","AVY","AXON","BKR","BALL","BAC","BAX",
    "BDX","BRK.B","BBY","TECH","BIIB","BLK","BX","XYZ","BNY","BA",
    "BKNG","BSX","BMY","AVGO","BR","BRO","BF.B","BLDR","BG","BXP",
    "CHRW","CDNS","CPT","CPB","COF","CAH","CCL","CARR","CVNA","CASY",
    "CAT","CBOE","CBRE","CDW","COR","CNC","CNP","CF","CRL","SCHW",
    "CHTR","CVX","CMG","CB","CHD","CIEN","CI","CINF","CTAS","CSCO",
    "C","CFG","CLX","CME","CMS","KO","CTSH","COHR","COIN","CL",
    "CMCSA","FIX","CAG","COP","ED","STZ","CEG","COO","CPRT","GLW",
    "CPAY","CTVA","CSGP","COST","CRH","CRWD","CCI","CSX","CMI","CVS",
    "DHR","DRI","DDOG","DVA","DECK","DE","DELL","DAL","DVN","DXCM",
    "FANG","DLR","DG","DLTR","D","DPZ","DASH","DOV","DOW","DHI",
    "DTE","DUK","DD","ETN","EBAY","SATS","ECL","EIX","EW","EA",
    "ELV","EME","EMR","ETR","EOG","EPAM","EQT","EFX","EQIX","EQR",
    "ERIE","ESS","EL","EG","EVRG","ES","EXC","EXE","EXPE","EXPD",
    "EXR","XOM","FFIV","FDS","FICO","FAST","FRT","FDX","FIS","FITB",
    "FSLR","FE","FISV","F","FTNT","FTV","FOXA","FOX","BEN","FCX",
    "GRMN","IT","GE","GEHC","GEV","GEN","GNRC","GD","GIS","GM",
    "GPC","GILD","GPN","GL","GDDY","GS","HAL","HIG","HAS","HCA",
    "DOC","HSIC","HSY","HPE","HLT","HD","HON","HRL","HST","HWM",
    "HPQ","HUBB","HUM","HBAN","HII","IBM","IEX","IDXX","ITW","INCY",
    "IR","PODD","INTC","IBKR","ICE","IFF","IP","INTU","ISRG","IVZ",
    "INVH","IQV","IRM","JBHT","JBL","JKHY","J","JNJ","JCI","JPM",
    "KVUE","KDP","KEY","KEYS","KMB","KIM","KMI","KKR","KLAC","KHC",
    "KR","LHX","LH","LRCX","LVS","LDOS","LEN","LII","LLY","LIN",
    "LYV","LMT","L","LOW","LULU","LITE","LYB","MTB","MPC","MAR",
    "MRSH","MLM","MAS","MA","MKC","MCD","MCK","MDT","MRK","META",
    "MET","MTD","MGM","MCHP","MU","MSFT","MAA","MRNA","TAP","MDLZ",
    "MPWR","MNST","MCO","MS","MOS","MSI","MSCI","NDAQ","NTAP","NFLX",
    "NEM","NWSA","NWS","NEE","NKE","NI","NDSN","NSC","NTRS","NOC",
    "NCLH","NRG","NUE","NVDA","NVR","NXPI","ORLY","OXY","ODFL","OMC",
    "ON","OKE","ORCL","OTIS","PCAR","PKG","PLTR","PANW","PSKY","PH",
    "PAYX","PYPL","PNR","PEP","PFE","PCG","PM","PSX","PNW","PNC",
    "POOL","PPG","PPL","PFG","PG","PGR","PLD","PRU","PEG","PTC",
    "PSA","PHM","PWR","QCOM","DGX","Q","RL","RJF","RTX","O",
    "REG","REGN","RF","RSG","RMD","RVTY","HOOD","ROK","ROL","ROP",
    "ROST","RCL","SPGI","CRM","SNDK","SBAC","SLB","STX","SRE","NOW",
    "SHW","SPG","SWKS","SJM","SW","SNA","SOLV","SO","LUV","SWK",
    "SBUX","STT","STLD","STE","SYK","SMCI","SYF","SNPS","SYY","TMUS",
    "TROW","TTWO","TPR","TRGP","TGT","TEL","TDY","TER","TSLA","TXN",
    "TPL","TXT","TMO","TJX","TKO","TTD","TSCO","TT","TDG","TRV",
    "TRMB","TFC","TYL","TSN","USB","UBER","UDR","ULTA","UNP","UAL",
    "UPS","URI","UNH","UHS","VLO","VEEV","VTR","VLTO","VRSN","VRSK",
    "VZ","VRTX","VRT","VTRS","VICI","V","VST","VMC","WRB","GWW",
    "WAB","WMT","DIS","WBD","WM","WAT","WEC","WFC","WELL","WST",
    "WDC","WY","WSM","WMB","WTW","WDAY","WYNN","XEL","XYL","YUM",
]

# ═══════════════════════════════════════════════════════════════════════
# Core logic — incremental VWAP bands checker
# ═══════════════════════════════════════════════════════════════════════


class VWAPTracker:
    """Incremental VWAP + stdev tracker, session-resettable."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.sum_pv = 0.0
        self.sum_v = 0.0
        self.sum_p2v = 0.0
        self.n = 0

    def add(self, typical_price: float, volume: float):
        self.sum_pv += typical_price * volume
        self.sum_v += volume
        self.sum_p2v += typical_price * typical_price * volume
        self.n += 1

    @property
    def vwap(self) -> float:
        return self.sum_pv / self.sum_v if self.sum_v > 0 else float("nan")

    @property
    def stdev(self) -> float:
        if self.sum_v <= 0:
            return 0.0
        mean = self.vwap
        var = max(0.0, self.sum_p2v / self.sum_v - mean * mean)
        return math.sqrt(var)


def session_boundary(ts: datetime, is_crypto: bool) -> datetime:
    """Return the session anchor for a timestamp.

    Equity: midnight ET
    Crypto: midnight UTC
    """
    if is_crypto:
        return ts.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    else:
        # Equity uses ET — approximate as US/Eastern
        import zoneinfo
        et = zoneinfo.ZoneInfo("America/New_York")
        local = ts.astimezone(et)
        return local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)


def typical_price(high: float, low: float, close: float) -> float:
    return (high + low + close) / 3.0


def compute_sma(bars, window: int) -> float:
    """Simple ATR computation from a list of (high, low, close) tuples."""
    if len(bars) < window + 1:
        return 0.0
    tr_sum = 0.0
    for i in range(len(bars) - window, len(bars)):
        h, l, pc = bars[i][0], bars[i][1], bars[i - 1][2]
        tr = max(h - l, abs(h - pc), abs(l - pc))
        tr_sum += tr
    return tr_sum / window


def run_single_sweep(symbol: str, asset_class: str, bars_list, timeframes_results: dict):
    """Run VWAP bands parameter sweep on one symbol's bars.

    bars_list: list of (ts, open, high, low, close, volume) sorted by ts
    timeframes_results: dict to populate with {timeframe_name: {combo_key: stats}}
    """
    is_crypto = asset_class == "crypto"

    # Tuple format: (ts, open, high, low, close, volume)
    ts_arr = np.array([b[0] for b in bars_list])
    o_arr = np.array([b[1] for b in bars_list])
    h_arr = np.array([b[2] for b in bars_list])
    l_arr = np.array([b[3] for b in bars_list])
    c_arr = np.array([b[4] for b in bars_list])
    v_arr = np.array([b[5] for b in bars_list])
    tp_arr = (h_arr + l_arr + c_arr) / 3.0

    n = len(bars_list)
    if n < 20:
        return  # not enough data

    # Precompute session boundaries
    sess = np.array([session_boundary(ts, is_crypto).timestamp() for ts in ts_arr])

    for sigma in SIGMA_VALUES:
        vwap = VWAPTracker()
        prev_sess = None

        # Per-bar VWAP, stdev, ATR(14)
        vwap_vals = np.full(n, float("nan"))
        stdev_vals = np.full(n, float("nan"))
        atr_vals = np.full(n, 0.0)

        rolling = []  # list of (h, l, c)

        for i in range(n):
            cur_sess = sess[i]
            if prev_sess is not None and cur_sess != prev_sess:
                vwap.reset()
            prev_sess = cur_sess

            vwap.add(tp_arr[i], v_arr[i])
            vwap_vals[i] = vwap.vwap
            stdev_vals[i] = vwap.stdev

            rolling.append((h_arr[i], l_arr[i], c_arr[i]))
            if len(rolling) > 14:
                rolling.pop(0)
            if len(rolling) >= 2:
                atr_vals[i] = compute_atr_from_list(rolling)

        # Find entry bars for this sigma
        for i in range(1, n):
            stdev_i = stdev_vals[i]
            if stdev_i <= 0 or math.isnan(stdev_i):
                continue
            vwap_i = vwap_vals[i]
            upper_i = vwap_i + sigma * stdev_i
            lower_i = vwap_i - sigma * stdev_i

            signals = []
            # Short: price spiked above band, closed back in
            if h_arr[i - 1] > upper_i and c_arr[i] < upper_i:
                entry = c_arr[i]
                atr_i = atr_vals[i] if atr_vals[i] > 0 else abs(c_arr[i]) * 0.01
                signals.append(("short", entry, atr_i, vwap_i, i))
            # Long: price dipped below band, closed back in
            if l_arr[i - 1] < lower_i and c_arr[i] > lower_i:
                entry = c_arr[i]
                atr_i = atr_vals[i] if atr_vals[i] > 0 else abs(c_arr[i]) * 0.01
                signals.append(("long", entry, atr_i, vwap_i, i))

            for side, entry, atr_i, vwap_i, idx in signals:
                for atr_mult in ATR_MULT_STOP_VALUES:
                    # Stop depends on atr_mult
                    if side == "short":
                        stop = entry + atr_mult * atr_i
                    else:
                        stop = entry - atr_mult * atr_i
                    risk = abs(entry - stop)
                    if risk <= 0:
                        continue

                    for target_r in TARGET_R_VALUES:
                        if side == "short":
                            target = entry - target_r * risk
                        else:
                            target = entry + target_r * risk

                        outcome = simulate_trade(
                            side, entry, stop, target,
                            h_arr, l_arr, c_arr, idx + 1, n, 48
                        )

                        key = f"\u03c3={sigma}|stop={atr_mult}|R={target_r}"
                        if key not in timeframes_results:
                            timeframes_results[key] = {
                                "trades": 0, "wins": 0, "losses": 0,
                                "total_pnl_r": 0.0,
                            }
                        timeframes_results[key]["trades"] += 1
                        if outcome == "win":
                            timeframes_results[key]["wins"] += 1
                            timeframes_results[key]["total_pnl_r"] += target_r
                        elif outcome == "loss":
                            timeframes_results[key]["losses"] += 1
                            timeframes_results[key]["total_pnl_r"] -= 1.0
                        # timeout = 0 R, counts as neither win nor loss


def compute_atr_from_list(rolling: list) -> float:
    """Compute ATR(14) from list of (high, low, close) tuples."""
    if len(rolling) < 2:
        return 0.0
    trs = []
    for i in range(1, len(rolling)):
        h, l, c = rolling[i]
        pc = rolling[i - 1][2]
        tr = max(h - l, abs(h - pc), abs(l - pc))
        trs.append(tr)
    if not trs:
        return 0.0
    # Use last min(len, 14) values
    n = min(len(trs), 14)
    return sum(trs[-n:]) / n


def simulate_trade(side, entry, stop, target, h_arr, l_arr, c_arr, start_idx, end_idx, max_bars):
    """Simulate a trade: scan forward for stop or target hit first."""
    if side == "short":
        # Short: stop is above (max price), target is below (min price)
        for j in range(start_idx, min(start_idx + max_bars, end_idx)):
            if h_arr[j] >= stop:
                return "loss"  # stop hit
            if l_arr[j] <= target:
                return "win"  # target hit
    else:
        # Long: stop is below, target is above
        for j in range(start_idx, min(start_idx + max_bars, end_idx)):
            if l_arr[j] <= stop:
                return "loss"
            if h_arr[j] >= target:
                return "win"
    return "timeout"


def run_single_sweep_return(symbol: str, asset_class: str, bars_list) -> dict:
    """Wrapper that returns a results dict instead of mutating in-place."""
    results = {}
    run_single_sweep(symbol, asset_class, bars_list, results)
    return results


# ═══════════════════════════════════════════════════════════════════════
# Data fetching
# ═══════════════════════════════════════════════════════════════════════

def fetch_bars_batch(symbols_with_class, data_client, timeframe, start, end):
    """Fetch bars for a batch of symbols. Caches automatically."""
    data_dir = Path(PROJECT) / "runtime" / "bars_cache"
    data_dir.mkdir(parents=True, exist_ok=True)

    bars = {}
    for sym, ac in symbols_with_class:
        safe = sym.replace("/", "-")
        tf_short = timeframe.replace("Min", "m").replace("H", "h")
        cache_key = f"{safe}_{tf_short}_{start.date()}_{end.date()}.parquet"
        cache_path = data_dir / cache_key

        if cache_path.exists():
            try:
                df = pd.read_parquet(cache_path)
                bs = [(row.ts.to_pydatetime(), row.open, row.high, row.low, row.close, row.volume)
                      for row in df.itertuples(index=False)]
                if len(bs) > 0:
                    bars[sym] = bs
                    print(f"  [cache] {sym} ({ac}) — {len(bs)} bars")
                    continue
            except Exception:
                pass

        try:
            api_sym = normalize_for_api(sym, ac)
            if ac == "equity":
                raw = data_client.client.get_stock_bars(api_sym, timeframe, start, end)
            else:
                raw = data_client.client.get_crypto_bars(api_sym, timeframe, start, end)

            bs = []
            for r in raw:
                try:
                    ts = datetime.fromisoformat(r["t"].replace("Z", "+00:00"))
                    bs.append((ts, float(r["o"]), float(r["h"]), float(r["l"]), float(r["c"]), float(r["v"])))
                except (KeyError, ValueError):
                    pass

            if bs:
                # Write cache
                df = pd.DataFrame(bs, columns=["ts", "open", "high", "low", "close", "volume"])
                df.to_parquet(cache_path, index=False)
                bars[sym] = bs
                print(f"  [fetch] {sym} ({ac}) — {len(bs)} bars")
            else:
                print(f"  [skip] {sym} — no bars returned")
        except Exception as e:
            print(f"  [error] {sym}: {e}")

    return bars


def main():
    parser = argparse.ArgumentParser(description="VWAP Bands Sweep")
    parser.add_argument("--timeframes", default=",".join(TIMEFRAMES),
                        help="Comma-separated timeframes (default: all)")
    parser.add_argument("--start", default="2025-01-01",
                        help="Backtest start date (default: 2025-01-01)")
    parser.add_argument("--end", default="2026-05-26",
                        help="Backtest end date (default: 2026-05-26)")
    parser.add_argument("--equity-symbols", type=int, default=500,
                        help="Number of equity symbols to use (default: 500)")
    parser.add_argument("--crypto-symbols", type=int, default=10,
                        help="Number of crypto symbols to use (default: 10)")
    parser.add_argument("--max-workers", type=int, default=1,
                        help="Parallel backtest workers (default: 1)")
    parser.add_argument("--cache-only", action="store_true",
                        help="Only fetch and cache bars, don't run backtest")
    parser.add_argument("--skip-fetch", action="store_true",
                        help="Skip data fetch, only run backtest on cached data")
    args = parser.parse_args()

    timeframes = [tf.strip() for tf in args.timeframes.split(",")]
    start_dt = datetime.fromisoformat(args.start).replace(tzinfo=timezone.utc)
    end_dt = datetime.fromisoformat(args.end).replace(tzinfo=timezone.utc)

    # Build symbol list
    equity_syms = SP500_SYMBOLS[:min(args.equity_symbols, len(SP500_SYMBOLS))]
    crypto_syms = CRYPTO_SYMBOLS[:min(args.crypto_symbols, len(CRYPTO_SYMBOLS))]

    print(f"Symbol universe: {len(equity_syms)} equities + {len(crypto_syms)} crypto")
    print(f"Timeframes: {timeframes}")
    print(f"Date range: {start_dt.date()} → {end_dt.date()}")
    print(f"Parameter grid: {len(SIGMA_VALUES)} σ × {len(ATR_MULT_STOP_VALUES)} stop × {len(TARGET_R_VALUES)} R = {len(SIGMA_VALUES) * len(ATR_MULT_STOP_VALUES) * len(TARGET_R_VALUES)} combos")
    print()

    # Setup data client
    client = AlpacaClient()
    data = AlpacaData(client, cache_dir=str(Path(PROJECT) / "runtime" / "bars_cache"))

    # ── Phase 1: fetch bars ──────────────────────────────────────
    all_bars = {}  # {sym: {tf: [(ts, o, h, l, c, v), ...]}}

    for tf in timeframes:
        print(f"\n{'='*60}")
        print(f"  Timeframe: {tf}")
        print(f"{'='*60}")

        # Fetch equity
        eq_symbols = [(s, "equity") for s in equity_syms]
        print(f"  Fetching {len(eq_symbols)} equity symbols...")
        eq_bars = fetch_bars_batch(eq_symbols, data, tf, start_dt, end_dt)

        # Fetch crypto
        cr_symbols = [(s, "crypto") for s in crypto_syms]
        print(f"  Fetching {len(cr_symbols)} crypto symbols...")
        cr_bars = fetch_bars_batch(cr_symbols, data, tf, start_dt, end_dt)

        all_bars[tf] = {**eq_bars, **cr_bars}

        total_bars = sum(len(bs) for bs in all_bars[tf].values())
        print(f"  Total bars cached: {total_bars:,} across {len(all_bars[tf])} symbols")

    if args.cache_only:
        print("\nCache-only mode. Done.")
        return

    # ── Phase 2: run sweep per timeframe ─────────────────────────
    print(f"\n{'='*60}")
    print("  RUNNING PARAMETER SWEEP")
    print(f"{'='*60}")

    results = {}

    for tf in timeframes:
        print(f"\n--- Timeframe: {tf} ---")
        tf_bars = all_bars[tf]
        n_syms = len(tf_bars)
        if n_syms == 0:
            print("  No bars available. Skipping.")
            continue

        print(f"  Running sweep across {n_syms} symbols...")

        # Prepare per-symbol bar data
        per_symbol_data = []
        bar_counts = []
        for sym, bs in tf_bars.items():
            is_cr = sym in crypto_syms
            ac = "crypto" if is_cr else "equity"
            per_symbol_data.append((sym, ac, bs))

        # Build results container
        results[tf] = {}
        per_symbol_results = {}

        if args.max_workers > 1:
            with ProcessPoolExecutor(max_workers=args.max_workers) as pool:
                future_to_sym = {}
                for sym, ac, bs in per_symbol_data:
                    fut = pool.submit(run_single_sweep_return, sym, ac, bs)
                    future_to_sym[fut] = sym

                for fut in as_completed(future_to_sym):
                    sym = future_to_sym[fut]
                    try:
                        per_symbol_results[sym] = fut.result()
                    except Exception as e:
                        print(f"  [error] {sym}: {e}")
        else:
            for sym, ac, bs in per_symbol_data:
                per_symbol_results[sym] = run_single_sweep_return(sym, ac, bs)

        # Merge per-symbol results into aggregated dict
        for sym, sym_results in per_symbol_results.items():
            if sym_results is None:
                continue
            for key, stats in sym_results.items():
                if key not in results[tf]:
                    results[tf][key] = {"trades": 0, "wins": 0, "losses": 0, "total_pnl_r": 0.0}
                results[tf][key]["trades"] += stats["trades"]
                results[tf][key]["wins"] += stats["wins"]
                results[tf][key]["losses"] += stats["losses"]
                results[tf][key]["total_pnl_r"] += stats["total_pnl_r"]

        # Aggregate results for this timeframe
        print(f"  Aggregating {len(results[tf])} parameter combos...")
        agg = {}
        for key, stats in results[tf].items():
            if stats["trades"] >= 10:
                wr = stats["wins"] / stats["trades"] if stats["trades"] > 0 else 0.0
                avg_r = stats["total_pnl_r"] / stats["trades"] if stats["trades"] > 0 else 0.0
                agg[key] = {
                    "trades": stats["trades"],
                    "wins": stats["wins"],
                    "losses": stats["losses"],
                    "win_rate": round(wr, 4),
                    "avg_R": round(avg_r, 4),
                    "total_R": round(stats["total_pnl_r"], 2),
                }

        results[tf] = agg

        # Print best results
        sorted_combos = sorted(agg.items(), key=lambda x: (-x[1]["win_rate"], -x[1]["trades"]))
        print(f"\n  TOP COMBOS (sorted by win rate, min 10 trades):")
        print(f"  {'Rank':<6} {'Params':<32} {'Trades':<8} {'WR':<8} {'Avg R':<8} {'Total R':<10}")
        print(f"  {'-'*72}")
        for rank, (key, s) in enumerate(sorted_combos[:15], 1):
            print(f"  {rank:<6} {key:<32} {s['trades']:<8} {s['win_rate']:<8.2%} {s['avg_R']:<+8.4f} {s['total_R']:<+10.2f}")

    # ── Summary ───────────────────────────────────────────────────
    print(f"\n{'='*60}")
    print("  FINAL SUMMARY: BEST PARAM PER TIMEFRAME")
    print(f"{'='*60}")
    print(f"  {'Timeframe':<12} {'Best Params':<32} {'Trades':<8} {'WR':<8} {'Avg R':<10}")
    print(f"  {'-'*70}")

    for tf in timeframes:
        if tf not in results or not results[tf]:
            print(f"  {tf:<12} {'No data':<32}")
            continue
        sorted_c = sorted(results[tf].items(), key=lambda x: (-x[1]["win_rate"], -x[1]["trades"]))
        if sorted_c:
            best_key, best_s = sorted_c[0]
            print(f"  {tf:<12} {best_key:<32} {best_s['trades']:<8} {best_s['win_rate']:<8.2%} {best_s['avg_R']:<+10.4f}")

    # Save full results to JSON
    out_path = Path(PROJECT) / "runtime" / "vwap_bands_sweep_results.json"
    serializable = {}
    for tf, agg in results.items():
        serializable[tf] = {k: v for k, v in agg.items()}
    with open(out_path, "w") as f:
        json.dump(serializable, f, indent=2, default=str)
    print(f"\nFull results saved to: {out_path}")

    # Also save a human-readable summary
    summary_path = Path(PROJECT) / "runtime" / "vwap_bands_sweep_summary.txt"
    with open(summary_path, "w") as f:
        f.write("VWAP Bands Parameter Sweep Results\n")
        f.write("=" * 60 + "\n")
        f.write(f"Universe: {len(equity_syms)} equities + {len(crypto_syms)} crypto\n")
        f.write(f"Date range: {start_dt.date()} → {end_dt.date()}\n\n")
        for tf in timeframes:
            if tf not in results or not results[tf]:
                continue
            f.write(f"\n--- {tf} ---\n")
            sorted_c = sorted(results[tf].items(), key=lambda x: (-x[1]["win_rate"], -x[1]["trades"]))
            f.write(f"{'Params':<32} {'Trades':<8} {'WR':<8} {'Avg R':<10}\n")
            f.write("-" * 58 + "\n")
            for key, s in sorted_c[:20]:
                f.write(f"{key:<32} {s['trades']:<8} {s['win_rate']:<7.1%} {s['avg_R']:<+10.4f}\n")
    print(f"Summary saved to: {summary_path}")

    print("\nDone.")


if __name__ == "__main__":
    main()