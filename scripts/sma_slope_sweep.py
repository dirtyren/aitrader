#!/usr/bin/env python3
"""
SMA-Slope parameter grid sweep for TQQQ (daily, long-only).

Sweeps SMA period N x slope lookback k and ranks combos by a risk-adjusted
composite (Calmar = CAGR / |MaxDD|) with Sharpe as a tiebreaker, since the
user's targets emphasise max DD and Sharpe. Reports every combo plus the
best combo overall.

Run:  python scripts/sma_slope_sweep.py
"""

from __future__ import annotations

import os
import sys

# Allow importing the sibling backtest module regardless of package layout.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import sma_slope_backtest as ssb  # noqa: E402

# Grid — tune these to widen/narrow the sweep.
SMA_GRID = [50, 100, 150, 200, 250]
SLOPE_GRID = [2, 3, 5, 10, 20]

SYMBOL = "TQQQ"
START = "2018-01-01"
END = "2026-08-28"
CAPITAL = 100_000.0
SLIPPAGE_BPS = 2.0
FEE_RATE = 0.0
COMMISSION_PER_SHARE = 0.0


def main() -> int:
    df = ssb.load_bars(SYMBOL, START, END)
    close = df["close"].to_numpy(dtype=float)

    rows: list[dict] = []
    for n in SMA_GRID:
        for k in SLOPE_GRID:
            if len(df) < n + k + 10:
                continue
            signal = ssb.compute_signals(close, n, k)
            result = ssb.run_backtest(
                df, signal,
                initial_capital=CAPITAL,
                slippage_bps=SLIPPAGE_BPS,
                commission_per_share=COMMISSION_PER_SHARE,
                fee_rate=FEE_RATE,
            )
            m = ssb.compute_metrics(result["equity_curve"], result["trades"], CAPITAL)
            cagr = (m["cagr_pct"] or 0.0)
            max_dd = abs(m["max_drawdown_pct"]) or 1e-9
            calmar = cagr / max_dd
            rows.append({
                "N": n, "k": k,
                "trades": m["trade_count"],
                "ret%": m["total_return_pct"],
                "cagr%": round(cagr, 2),
                "sharpe": m["sharpe"],
                "maxdd%": m["max_drawdown_pct"],
                "wr%": m.get("win_rate_pct"),
                "calmar": round(calmar, 3),
            })

    rows.sort(key=lambda r: r["calmar"], reverse=True)

    # Header
    hdr = f"{'N':>4} {'k':>3} {'trades':>6} {'ret%':>9} {'cagr%':>7} {'sharpe':>7} {'maxdd%':>8} {'wr%':>6} {'calmar':>7}"
    print("=" * len(hdr))
    print(f"SMA-slope sweep  {SYMBOL}  {START} -> {END}  (capital=${CAPITAL:,.0f}, slippage={SLIPPAGE_BPS}bps)")
    print("=" * len(hdr))
    print(hdr)
    print("-" * len(hdr))
    for r in rows:
        print(f"{r['N']:>4} {r['k']:>3} {r['trades']:>6} {r['ret%']:>9.2f} {r['cagr%']:>7.2f} {r['sharpe']:>7.2f} {r['maxdd%']:>8.2f} {r['wr%']:>6.1f} {r['calmar']:>7.3f}")
    print("-" * len(hdr))

    best = rows[0]
    print("\nBEST COMBO (by Calmar):")
    print(f"  SMA(N={best['N']})  slope(k={best['k']})")
    print(f"  CAGR {best['cagr%']}%  Sharpe {best['sharpe']}  MaxDD {best['maxdd%']}%  trades={best['trades']}")
    print(f"  Total return {best['ret%']}%  Win rate {best['wr%']}%")

    # Also report the best by Sharpe and by max-DD floor for context.
    best_sharpe = max(rows, key=lambda r: r["sharpe"])
    print(f"\nBest by Sharpe : N={best_sharpe['N']} k={best_sharpe['k']}  Sharpe={best_sharpe['sharpe']}  MaxDD={best_sharpe['maxdd%']}%")
    min_dd = min(rows, key=lambda r: abs(r["maxdd%"]))
    print(f"Lowest MaxDD   : N={min_dd['N']} k={min_dd['k']}  MaxDD={min_dd['maxdd%']}%  Sharpe={min_dd['sharpe']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
