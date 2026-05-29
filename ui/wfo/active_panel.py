"""Active Overrides panel: read-only list of symbols.<sym> in active YAML
plus a Revert button per row, audit-trail tail, and a 'next-restart' banner."""
from __future__ import annotations
from pathlib import Path

import pandas as pd
import streamlit as st

from ui.wfo.approval import read_active, revert_symbol, read_audit_tail


def render(active_path: Path, audit_path: Path, runs_root: Path) -> None:
    st.info("Changes here take effect on the next trader restart.")

    payload = read_active(active_path)
    syms = payload.get("symbols") or {}

    if not syms:
        st.caption("No active overrides. Live trader uses its strategy yaml defaults.")
    else:
        rows = []
        for s, entry in syms.items():
            params = entry.get("setup_params") or {}
            prov = entry.get("_provenance") or {}
            rows.append({
                "Symbol": s,
                "Timeframe": entry.get("timeframe"),
                "Setup": entry.get("setup"),
                "Params": ", ".join(f"{k}={v}" for k, v in params.items()),
                "Run": prov.get("run_id"),
                "Approved at": prov.get("approved_at"),
            })
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

        revert_target = st.selectbox(
            "Revert symbol", options=[""] + list(syms.keys()))
        if revert_target and st.button(f"Revert {revert_target}"):
            revert_symbol(active_path, audit_path, revert_target)
            st.success(f"Reverted {revert_target}.")
            st.rerun()

    with st.expander("Audit trail (last 50)"):
        tail = read_audit_tail(audit_path, n=50)
        if not tail:
            st.caption("No audit entries yet.")
        else:
            st.dataframe(pd.DataFrame(tail),
                         use_container_width=True, hide_index=True)
