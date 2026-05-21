"""Top-level WFO-tab entry point. Switches between the four panels via
st.session_state['wfo_panel']."""
from __future__ import annotations
from pathlib import Path

import streamlit as st

from ui.wfo import active_panel, forms, run_detail, runs_list, supervisor


_RUNS_ROOT = Path("runtime/wfo")
_JOBS_ROOT = _RUNS_ROOT / "jobs"
_ACTIVE_DIR = _RUNS_ROOT / "active"
_ACTIVE_FILE = _ACTIVE_DIR / "live_overrides.yaml"
_AUDIT_FILE = _ACTIVE_DIR / "audit.jsonl"


def render() -> None:
    # Boot the supervisor thread (idempotent).
    supervisor.get_or_start_supervisor(_JOBS_ROOT)

    panel = st.session_state.get("wfo_panel", "runs_list")
    cols = st.columns(4)
    if cols[0].button("Runs", use_container_width=True,
                      type=("primary" if panel in ("runs_list", "run_detail")
                            else "secondary")):
        st.session_state["wfo_panel"] = "runs_list"
        st.rerun()
    if cols[1].button("New Run", use_container_width=True,
                      type=("primary" if panel == "new_run" else "secondary")):
        st.session_state["wfo_panel"] = "new_run"
        st.rerun()
    if cols[2].button("Active overrides", use_container_width=True,
                      type=("primary" if panel == "active" else "secondary")):
        st.session_state["wfo_panel"] = "active"
        st.rerun()
    cols[3].empty()
    st.divider()

    panel = st.session_state.get("wfo_panel", "runs_list")
    _RUNS_ROOT.mkdir(parents=True, exist_ok=True)
    _ACTIVE_DIR.mkdir(parents=True, exist_ok=True)

    if panel == "runs_list":
        runs_list.render(_JOBS_ROOT, _RUNS_ROOT)
    elif panel == "run_detail":
        run_id = st.session_state.get("wfo_selected_run")
        if not run_id:
            st.session_state["wfo_panel"] = "runs_list"
            st.rerun()
        else:
            run_detail.render(run_id, _RUNS_ROOT, _ACTIVE_FILE, _AUDIT_FILE)
    elif panel == "new_run":
        forms.render_form(_JOBS_ROOT)
    elif panel == "active":
        active_panel.render(_ACTIVE_FILE, _AUDIT_FILE, _RUNS_ROOT)
