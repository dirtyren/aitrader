#!/usr/bin/env python3
"""Quick per-strategy stats from the MySQL aitrader database.

Usage:
    python scripts/strategy_stats.py           # all strategies, last 30 days
    python scripts/strategy_stats.py --days 7  # last 7 days
    python scripts/strategy_stats.py --live    # open positions snapshot
"""

import argparse
import os
import sys
from urllib.parse import quote_plus as urlquote

def main():
    parser = argparse.ArgumentParser(description="aitrader per-strategy stats")
    parser.add_argument("--days", type=int, default=30, help="Lookback window (default: 30)")
    parser.add_argument("--live", action="store_true", help="Show currently open positions")
    args = parser.parse_args()

    import pymysql

    conn = pymysql.connect(
        host=os.environ.get("MYSQL_HOST", "mysql"),
        port=int(os.environ.get("MYSQL_PORT", "3306")),
        user=os.environ.get("MYSQL_USER", "trader"),
        password=os.environ.get("MYSQL_PASSWORD", "traderpass"),
        database=os.environ.get("MYSQL_DATABASE", "aitrader"),
        charset="utf8mb4",
    )

    try:
        with conn.cursor() as cur:
            if args.live:
                cur.execute("""
                    SELECT s.name AS strategy, p.symbol, p.side, p.qty,
                           p.entry_px, p.stop_px, p.target_px,
                           p.setup_name, p.opened_at, p.adopted
                    FROM positions p
                    JOIN strategies s ON p.strategy_id = s.id
                    WHERE p.status = 'open'
                    ORDER BY s.name, p.opened_at
                """)
                rows = cur.fetchall()
                if not rows:
                    print("No open positions.")
                else:
                    print(f"{'Strategy':<20} {'Symbol':<12} {'Side':<6} {'Qty':>14} {'Entry':>12} {'Stop':>12} {'Target':>12} {'Setup':<16} {'Adopted':<8}")
                    print("-" * 118)
                    for r in rows:
                        adopted = "yes" if r[9] else "no"
                        print(f"{r[0]:<20} {r[1]:<12} {r[2]:<6} {float(r[3]):>14.6f} {float(r[4]):>12.4f} {float(r[5]) if r[5] else 'N/A':>12} {float(r[6]) if r[6] else 'N/A':>12} {r[7]:<16} {adopted:<8}")
            else:
                cur.execute("""
                    SELECT s.name AS strategy,
                           COUNT(*) AS total,
                           SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END) AS wins,
                           SUM(CASE WHEN pnl_usd < 0 THEN 1 ELSE 0 END) AS losses,
                           ROUND(COALESCE(SUM(pnl_usd), 0), 2) AS total_pnl,
                           ROUND(COALESCE(AVG(R_realized), 0), 2) AS avg_R,
                           MAX(closed_at) AS last_trade,
                           COUNT(CASE WHEN reflected=0 THEN 1 END) AS unreflected
                    FROM trades t
                    JOIN strategies s ON t.strategy_id = s.id
                    WHERE t.closed_at >= DATE_SUB(NOW(), INTERVAL %s DAY)
                    GROUP BY s.name
                    ORDER BY total DESC
                """, (args.days,))
                rows = cur.fetchall()
                if not rows:
                    strategy_cur = conn.cursor()
                    strategy_cur.execute("SELECT name FROM strategies ORDER BY name")
                    strategies = [r[0] for r in strategy_cur.fetchall()]
                    print(f"{'Strategy':<20} {'Trades':>8} {'Wins':>6} {'Losses':>8} {'Total PnL':>12} {'Avg R':>8} {'Unreflected':>12}")
                    print("-" * 78)
                    for s in strategies:
                        print(f"{s:<20} {'0':>8} {'0':>6} {'0':>8} {'0.00':>12} {'0.00':>8} {'0':>12}")
                else:
                    print(f"Stats over last {args.days} days:")
                    print(f"{'Strategy':<20} {'Trades':>8} {'Wins':>6} {'Losses':>8} {'Total PnL':>12} {'Avg R':>8} {'Last Trade':<20} {'Unreflected':>12}")
                    print("-" * 100)
                    for r in rows:
                        last = str(r[6])[:19] if r[6] else "N/A"
                        print(f"{r[0]:<20} {r[1]:>8} {r[2]:>6} {r[3]:>8} {float(r[4]):>12.2f} {float(r[5]):>8.2f} {last:<20} {r[7]:>12}")
    finally:
        conn.close()

if __name__ == "__main__":
    main()
