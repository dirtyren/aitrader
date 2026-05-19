"""VWAP Wave dashboard.

Reads runtime/trading_state.json (written by main.py at the end of each
scheduler tick) and renders three panels:
  - top bar: equity, day P&L, circuit level, as-of timestamp
  - per-symbol table: regime, VWAP, ±σ bands, open position
  - recent filter rejects
"""
import json
from pathlib import Path

import pandas as pd
import streamlit as st
from streamlit_autorefresh import st_autorefresh

STATE_FILE = Path("runtime/trading_state.json")

st.set_page_config(page_title="VWAP Wave", layout="wide")
st_autorefresh(interval=5_000, key="vwap_wave_refresh")
st.title("VWAP Wave Protocol")


def _read_state() -> dict | None:
    if not STATE_FILE.exists():
        return None
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return None


state = _read_state()
if state is None:
    st.warning("No state file yet. Start the engine via `python main.py`.")
    st.stop()

# --- Header strip
col1, col2, col3, col4 = st.columns(4)
col1.metric("Equity", f"${state['equity']:,.2f}")
col2.metric("Day P&L", f"${state['day_pnl']:,.2f}")
col3.metric("Circuit Level", state["circuit_level"])
col4.metric("As of", state["timestamp"])

# --- Per-symbol table
st.subheader("Symbols")
rows = []
for s in state.get("symbols", []):
    pos = s.get("open_position")
    rows.append({
        "Symbol": s["symbol"],
        "Regime": s.get("regime"),
        "VWAP": s.get("vwap"),
        "Upper σ": s.get("upper"),
        "Lower σ": s.get("lower"),
        "Position": (f"{pos['side']} {pos['qty']} @ {pos['entry']}" if pos else "—"),
        "Stop": pos["stop"] if pos else "",
        "Target": pos["target"] if pos else "",
    })
df = pd.DataFrame(rows)
st.dataframe(df, use_container_width=True, hide_index=True)

# --- Filter audit
st.subheader("Recent filter rejects")
rejects = state.get("recent_filter_rejects", [])
if rejects:
    st.dataframe(pd.DataFrame(rejects), use_container_width=True, hide_index=True)
else:
    st.caption("No rejects in the recent window.")
