#!/usr/bin/env python3
"""Check unreflected trades per strategy in MySQL."""
import pymysql

conn = pymysql.connect(
    host='mysql',
    user='trader',
    password='traderpass',
    database='aitrader',
    connect_timeout=5
)

with conn.cursor() as cur:
    cur.execute("""
        SELECT s.name AS strategy, COUNT(*) AS unreflected 
        FROM trades t 
        JOIN strategies s ON t.strategy_id=s.id 
        WHERE t.reflected=0 
        GROUP BY s.name
    """)
    rows = cur.fetchall()
    if rows:
        for row in rows:
            print(f"{row[0]}: {row[1]} unreflected trades")
    else:
        print("No unreflected trades found for any strategy")

conn.close()
