"""VWAP Wave dashboard.

Two tabs:
  - Overview: equity, day P&L, circuit level, per-symbol regime/VWAP
    table, and recent filter rejects (sourced from runtime/trading_state.json).
  - Logs:  tail of logs/vwap_wave.log with level filter and live toggle.
"""
import json
from pathlib import Path

import pandas as pd
import streamlit as st
import yaml
from streamlit_autorefresh import st_autorefresh

from ui.logs_panel import render as render_logs

STATE_FILE = Path("runtime/trading_state.json")
DEFAULT_LOG_FILE = Path("logs/vwap_wave.log")


def _read_state() -> dict | None:
    if not STATE_FILE.exists():
        return None
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return None


def _resolve_log_file() -> Path:
    cfg_path = Path("config/settings.yaml")
    if not cfg_path.exists():
        return DEFAULT_LOG_FILE
    try:
        cfg = yaml.safe_load(cfg_path.read_text()) or {}
    except Exception:
        return DEFAULT_LOG_FILE
    return Path(cfg.get("logging", {}).get("log_file") or DEFAULT_LOG_FILE)


st.set_page_config(page_title="VWAP Wave", layout="wide")
st_autorefresh(interval=5_000, key="vwap_wave_refresh")
st.title("VWAP Wave Protocol")

from ui.wfo import tab as wfo_tab

overview_tab, logs_tab, wfo_tab_panel = st.tabs(["Overview", "Logs", "WFO"])

with overview_tab:
    state = _read_state()
    if not state or "equity" not in state:
        st.warning("No state file yet. Start the engine via `python main.py`.")
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
    render_logs(_resolve_log_file())

with wfo_tab_panel:
    wfo_tab.render()
