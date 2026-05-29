"""VWAP Wave Dashboard — entry point.

Tabs: Strategies | Live Trading | Logs | WFO
Theme: dark, financial.
Auth/TLS: handled by the nginx reverse proxy in front of this app
(see nginx/ for config). This file assumes the request reaches it
already authenticated.
"""
from __future__ import annotations

import glob
import logging
import traceback
from pathlib import Path

import streamlit as st
from streamlit_autorefresh import st_autorefresh

from ui.components.theme import inject_theme
from ui.logging_setup import setup_logging
from ui.tabs import config_tab, live_tab, reconciliation_tab, strategies_tab
from ui.tabs.logs_panel import render as render_logs
from ui.wfo import tab as wfo_tab

_ASSETS = Path(__file__).parent / "assets"
_FAVICON = _ASSETS / "favicon.svg"
_LOGO = _ASSETS / "logo.svg"


_dashboard_logger = setup_logging(
    log_level=logging.INFO,
    log_file="logs/dashboard.log",
    logger_name="dashboard",
)


def _safe_render(name: str, fn) -> None:
    try:
        fn()
    except Exception:
        _dashboard_logger.error(
            "Tab %r failed:\n%s", name, traceback.format_exc()
        )
        st.error(f"`{name}` tab failed — see Logs tab (dashboard.log) for traceback.")


st.set_page_config(
    page_title="aitrader",
    page_icon=str(_FAVICON) if _FAVICON.exists() else None,
    layout="wide",
    initial_sidebar_state="collapsed",
)
inject_theme()
if _LOGO.exists():
    st.image(str(_LOGO), width=200)
else:
    st.title("aitrader")

strategies_t, live_t, recon_t, config_t, logs_t, wfo_t = st.tabs([
    "Strategies", "Live Trading", "Reconciliation", "Configuration", "Logs", "WFO",
])

with strategies_t:
    _safe_render("strategies", strategies_tab.render)

with live_t:
    st_autorefresh(interval=5_000, key="live_refresh")
    _safe_render("live", live_tab.render)

with recon_t:
    _safe_render("reconciliation", reconciliation_tab.render)

with config_t:
    _safe_render("config", config_tab.render)

with logs_t:
    state_files = sorted(glob.glob("runtime/trading_state_*.json"))
    strategies = [Path(f).stem.replace("trading_state_", "") for f in state_files]
    if Path("logs/dashboard.log").exists():
        strategies = ["dashboard", *strategies]
    if not strategies:
        st.info("No strategy state files found yet — start a trader service to see logs here.")
    else:
        selected = st.selectbox("Log source", strategies, key="logs_strategy")
        log_path = Path(f"logs/{selected}.log")
        if log_path.exists():
            render_logs(log_path)
        else:
            st.info(f"Log file not found at {log_path} yet.")

with wfo_t:
    _safe_render("wfo", wfo_tab.render)
