"""VWAP Wave dashboard.

Two tabs:
  - Overview: equity, day P&L, circuit level, per-symbol regime/VWAP
    table, and recent filter rejects (sourced from runtime/trading_state_*.json).
  - Logs:  tail of logs/*.log with level filter and live toggle.
"""
import json
import glob
from pathlib import Path

import pandas as pd
import streamlit as st
import yaml
from streamlit_autorefresh import st_autorefresh

from ui.tabs.logs_panel import render as render_logs

st.set_page_config(page_title="VWAP Wave Multi-Strategy Dashboard", layout="wide")

# Auto-refresh every 5 seconds
st_autorefresh(interval=5_000, key="vwap_wave_refresh")

st.title("VWAP Wave Quantitative Engine")

# 1. Dynamically find all trading states
state_files = glob.glob("runtime/trading_state_*.json")
strategies = []
for f in state_files:
    # e.g. "runtime/trading_state_rsi_trader.json" -> "rsi_trader"
    name = Path(f).stem.replace("trading_state_", "")
    strategies.append(name)

# Fallback/default if empty
if not strategies:
    strategies = ["vwap_wave"]
    if Path("runtime/trading_state.json").exists():
        state_files = ["runtime/trading_state.json"]
        strategies = ["vwap_wave"]

strategies = sorted(list(set(strategies)))

# Dropdown to select strategy
selected_strategy = st.sidebar.selectbox("Select Trading Strategy", strategies, index=0)

# Resolve state and log file paths based on selected strategy
if selected_strategy == "vwap_wave" and not Path(f"runtime/trading_state_{selected_strategy}.json").exists() and Path("runtime/trading_state.json").exists():
    STATE_FILE = Path("runtime/trading_state.json")
else:
    STATE_FILE = Path(f"runtime/trading_state_{selected_strategy}.json")

# Log files mapping
log_file_map = {
    "vwap_wave": Path("logs/vwap_wave.log"),
    "rsi_trader": Path("logs/rsi_trader.log"),
    "ib_trader": Path("logs/ib_trader.log"),
    "vwap_bands_trader": Path("logs/vwap_bands_trader.log"),
    "orb_trader": Path("logs/orb_trader.log"),
}
LOG_FILE = log_file_map.get(selected_strategy, Path(f"logs/{selected_strategy}.log"))


def _read_state() -> dict | None:
    if not STATE_FILE.exists():
        return None
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return None


from ui.wfo import tab as wfo_tab

overview_tab, logs_tab, wfo_tab_panel = st.tabs(["Overview", "Logs", "WFO"])

with overview_tab:
    state = _read_state()
    if not state or "equity" not in state:
        st.warning(f"No state file yet for strategy '{selected_strategy}'. Waiting for first cycle...")
    else:
        col1, col2, col3, col4 = st.columns(4)
        col1.metric("Equity", f"${state['equity']:,.2f}")
        col2.metric("Day P&L", f"${state['day_pnl']:,.2f}")
        col3.metric("Circuit Level", state["circuit_level"])
        col4.metric("As of", state["timestamp"])

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

        st.subheader("Recent filter rejects")
        rejects = state.get("recent_filter_rejects", [])
        if rejects:
            st.dataframe(pd.DataFrame(rejects), use_container_width=True, hide_index=True)
        else:
            st.caption("No rejects in the recent window.")

with logs_tab:
    if LOG_FILE.exists():
        render_logs(LOG_FILE)
    else:
        st.info(f"Log file not found at {LOG_FILE} yet.")

with wfo_tab_panel:
    wfo_tab.render()
