"""Runs-list panel: active-job banner, queue list, completed runs table.

Reads only — does not mutate jobs/* or runtime/wfo/*. Approve actions live in
run_detail.py; cancel/remove queue actions live here.
"""
from __future__ import annotations
import json
from pathlib import Path

import pandas as pd
import streamlit as st

from ui.wfo.job_state import (
    list_jobs, write_cancel_sentinel, JobRecord,
)


def _format_duration(rec: JobRecord) -> str:
    if rec.completed_at and rec.started_at:
        from datetime import datetime
        dt = (datetime.fromisoformat(rec.completed_at)
              - datetime.fromisoformat(rec.started_at))
        secs = int(dt.total_seconds())
        return f"{secs // 60}m {secs % 60}s"
    return "—"


def _read_run_meta(run_dir: Path) -> dict:
    out = {"universe_size": None, "evaluated": None, "passed": None}
    manifest = run_dir / "manifest.json"
    if manifest.exists():
        try:
            m = json.loads(manifest.read_text())
            out["evaluated"] = m.get("evaluated_groups")
            out["passed"] = m.get("passed_groups")
        except Exception:
            pass
    universe = run_dir / "universe.parquet"
    if universe.exists():
        try:
            out["universe_size"] = len(pd.read_parquet(universe))
        except Exception:
            pass
    return out


def render(jobs_root: Path, runs_root: Path) -> None:
    queued, active, history = list_jobs(jobs_root)

    # --- Active job banner ---
    if active:
        rec = active[0]
        st.subheader(f"⚙ Running: {rec.job_id}")
        p = rec.progress
        if p.total_pairs:
            ratio = (p.completed_pairs / p.total_pairs) if p.total_pairs else 0.0
            st.progress(min(max(ratio, 0.0), 1.0))
        cols = st.columns(4)
        cols[0].metric("Pairs", f"{p.completed_pairs}/{p.total_pairs or '?'}")
        cols[1].metric("Current",
                       f"{p.current_symbol or '—'} · {p.current_timeframe or '—'}")
        cols[2].metric("Elapsed", f"{int(p.elapsed_s)}s")
        cols[3].metric("ETA", f"{int(p.eta_s)}s" if p.eta_s else "—")
        if st.button("Cancel running job", type="secondary"):
            write_cancel_sentinel(jobs_root, rec.job_id)
            st.warning(f"Cancel requested for {rec.job_id}.")
        st.divider()

    # --- Queue ---
    if queued:
        st.subheader(f"Queued ({len(queued)})")
        for rec in queued:
            cols = st.columns([3, 6, 1])
            cols[0].text(rec.job_id)
            cols[1].text(f"queued at {rec.queued_at}")
            if cols[2].button("Remove", key=f"remove_{rec.job_id}"):
                (jobs_root / "queue" / f"{rec.job_id}.json").unlink(missing_ok=True)
                (jobs_root / "configs" / f"{rec.job_id}.yaml").unlink(missing_ok=True)
                st.rerun()
        st.divider()

    # --- Completed runs ---
    st.subheader("Runs")
    runs = sorted([d for d in runs_root.iterdir() if d.is_dir()
                   and d.name not in ("active", "jobs", "presets",
                                       "universe_cache", "latest")],
                  key=lambda p: p.name, reverse=True)
    if not runs:
        st.caption("No runs yet. Create one from **New Run**.")
        return

    history_by_id = {r.run_id: r for r in history}
    rows = []
    for d in runs:
        meta = _read_run_meta(d)
        hrec = history_by_id.get(d.name)
        rows.append({
            "run_id": d.name,
            "status": (hrec.status if hrec else "—"),
            "duration": (_format_duration(hrec) if hrec else "—"),
            "universe": meta["universe_size"] or "—",
            "evaluated": meta["evaluated"] or "—",
            "passed": meta["passed"] or "—",
        })
    df = pd.DataFrame(rows)
    st.dataframe(df, use_container_width=True, hide_index=True)

    chosen = st.selectbox("Open run", options=[""] + [r["run_id"] for r in rows],
                          index=0)
    if chosen:
        st.session_state["wfo_selected_run"] = chosen
        st.session_state["wfo_panel"] = "run_detail"
        st.rerun()
