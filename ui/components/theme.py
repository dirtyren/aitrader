"""Inject custom CSS for a dense, financial-platform look.

Call `inject_theme()` once at the top of dashboard.py, before any tab
content renders.
"""
from __future__ import annotations

_CSS = """
<style>
/* Tabular numerals so columns align in tables and metric tiles */
[data-testid="stMetric"], [data-testid="stMetricValue"],
[data-testid="stDataFrame"] *, .stDataFrame * {
  font-variant-numeric: tabular-nums;
}

/* Tighten metric tiles */
[data-testid="stMetric"] {
  background: #0f1422;
  border: 1px solid #1f2a3d;
  border-radius: 6px;
  padding: 10px 14px;
}
[data-testid="stMetricLabel"] { font-size: 11px; color: #9ca3af; text-transform: uppercase; letter-spacing: 0.04em; }
[data-testid="stMetricValue"] { font-size: 22px; font-weight: 600; }

/* Dense tables */
.stDataFrame [data-testid="stTable"] td,
.stDataFrame [data-testid="stTable"] th { padding: 4px 8px !important; }

/* Subtle row striping */
.stDataFrame tbody tr:nth-child(even) { background: #0f1422; }

/* Tabs more visible against dark bg */
[data-baseweb="tab-list"] { border-bottom: 1px solid #1f2a3d; }

/* PnL semantic colors used inline by format_pnl */
.pnl-pos { color: #10b981; }
.pnl-neg { color: #ef4444; }
.pnl-neu { color: #9ca3af; }
</style>
"""


def inject_theme() -> None:
    import streamlit as st
    st.markdown(_CSS, unsafe_allow_html=True)
