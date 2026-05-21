"""Streamlit Logs tab.

Renders a level-filtered tail of the trader log file, using
`ui.log_reader.tail` for the heavy lifting. Live-toggle and refresh
button are wired through st.session_state; the page-level
st_autorefresh in dashboard.py drives the periodic re-read.
"""
from __future__ import annotations
import html
from datetime import datetime
from pathlib import Path

import streamlit as st

from ui.log_reader import ParsedLine, tail

_LEVEL_COLORS = {
    "ERROR": "#ff4b4b",
    "WARNING": "#ffb84d",
    "INFO": "#4b9eff",
    "DEBUG": "#888888",
    "CRITICAL": "#ff4b4b",
    "UNKNOWN": "#aaaaaa",
}

_CSS = """
<style>
.log-row { font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
           font-size: 12px; padding: 2px 0; line-height: 1.4; }
.log-badge { display: inline-block; min-width: 60px; padding: 0 6px;
             margin-right: 6px; border-radius: 3px; color: white;
             font-weight: 600; text-align: center; }
.log-ts { color: #aaa; margin-right: 6px; }
.log-logger { color: #888; margin-right: 6px; }
.log-msg { white-space: pre-wrap; }
</style>
"""


def render(log_path: str | Path) -> None:
    st.markdown(_CSS, unsafe_allow_html=True)

    st.session_state.setdefault("logs_levels", ["INFO", "WARNING", "ERROR"])
    st.session_state.setdefault("logs_tail", 500)
    st.session_state.setdefault("logs_live", True)
    st.session_state.setdefault("logs_force_refresh", False)
    st.session_state.setdefault("logs_snapshot", ([], None))

    c1, c2, c3, c4 = st.columns([3, 2, 1, 1])
    levels = c1.multiselect(
        "Levels",
        options=["DEBUG", "INFO", "WARNING", "ERROR"],
        default=st.session_state["logs_levels"],
        key="logs_levels",
    )
    n = c2.number_input(
        "Tail size", min_value=100, max_value=5000,
        value=st.session_state["logs_tail"], step=100,
        key="logs_tail",
    )
    live = c3.toggle("Live", value=st.session_state["logs_live"], key="logs_live")
    if c4.button("Refresh now"):
        st.session_state["logs_force_refresh"] = True

    should_read = st.session_state["logs_force_refresh"] or live
    if should_read:
        snapshot = tail(log_path, n=int(n))
        st.session_state["logs_snapshot"] = (snapshot, datetime.now())
        st.session_state["logs_force_refresh"] = False

    snapshot, last_read = st.session_state["logs_snapshot"]
    status = "live" if live else "paused"
    last_read_str = last_read.strftime("%H:%M:%S") if last_read else "—"
    st.caption(f"Showing last {len(snapshot)} entries · {status} · last read {last_read_str}")

    if not snapshot:
        st.info(f"No log entries yet at `{log_path}`.")
        return

    filtered = [r for r in snapshot if r.level in levels or
                (r.level == "UNKNOWN" and "INFO" in levels)]
    rows_html = "\n".join(_render_row(r) for r in reversed(filtered))
    st.markdown(rows_html, unsafe_allow_html=True)


def _render_row(r: ParsedLine) -> str:
    color = _LEVEL_COLORS.get(r.level, _LEVEL_COLORS["UNKNOWN"])
    return (
        f'<div class="log-row">'
        f'<span class="log-badge" style="background:{color}">{html.escape(r.level)}</span>'
        f'<span class="log-ts">{html.escape(r.timestamp)}</span>'
        f'<span class="log-logger">{html.escape(r.logger)}</span>'
        f'<span class="log-msg">{html.escape(r.message)}</span>'
        f'</div>'
    )
