#!/usr/bin/env python3
"""Check Alpaca positions."""
import os, json, sys
from dotenv import load_dotenv
import requests

load_dotenv("config/.env")
key = os.environ["ALPACA_API_KEY"]
secret = os.environ["ALPACA_SECRET_KEY"]

r = requests.get(
    "https://paper-api.alpaca.markets/v2/positions",
    headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret},
    timeout=10,
)
data = r.json()
if isinstance(data, list):
    if not data:
        print("No open positions on broker")
    for p in data:
        print(f"{p['symbol']:12s} side={p['side']:5s} qty={float(p['qty']):>10.4f} entry={float(p['avg_entry_price']):>10.2f} current={float(p['current_price']):>10.2f} upnl={float(p['unrealized_pl']):>8.2f}")
else:
    print(f"ERROR: {data}")