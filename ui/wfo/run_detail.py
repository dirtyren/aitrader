"""Run Detail panel: per-symbol comparison table + per-symbol charts +
per-symbol Approve/Reject buttons. Reads runtime/wfo/<run_id>/."""
from __future__ import annotations
import json
from pathlib import Path

import pandas as pd
import streamlit as st
import yaml

from ui.wfo.approval import (
    approve_symbol, GateFailedApproveError, read_active,
)
from ui.wfo.charts import (
    walk_oos_curve, walk_oos_sharpe_bars, is_vs_oos_scatter,
    param_heatmap, pick_heatmap_axes,
    build_equity_fig, build_sharpe_bars_fig, build_is_oos_scatter_fig,
    build_heatmap_fig,
)


def _candidate_for(symbol: str, candidate: dict) -> dict | None:
    return (candidate.get("symbols") or {}).get(symbol)


def _format_params(d: dict) -> str:
    pairs = [f"{k}={v}" for k, v in d.items()]
    return ", ".join(pairs)


def render(run_id: str, runs_root: Path,
           active_path: Path, audit_path: Path) -> None:
    run_dir = runs_root / run_id
    if not run_dir.exists():
        st.error(f"Run {run_id} not found.")
        return

    cols = st.columns([5, 1])
    cols[0].subheader(f"Run {run_id}")
    if cols[1].button("Back to runs"):
        st.session_state["wfo_panel"] = "runs_list"
        st.rerun()

    candidate_path = run_dir / "live_overrides.yaml"
    candidate = (yaml.safe_load(candidate_path.read_text())
                 if candidate_path.exists() else {"symbols": {}})

    manifest_path = run_dir / "manifest.json"
    if manifest_path.exists():
        m = json.loads(manifest_path.read_text())
        st.caption(f"git_sha: {m.get('git_sha')} • "
                   f"evaluated: {m.get('evaluated_groups')} • "
                   f"passed: {m.get('passed_groups')}")

    parquet_path = run_dir / "results.parquet"
    if not parquet_path.exists():
        st.warning("results.parquet not found — run may have crashed.")
        return
    df = pd.read_parquet(parquet_path)

    active = read_active(active_path)
    rows = []
    candidate_symbols = list((candidate.get("symbols") or {}).keys())
    # Also surface gate-failed symbols not in candidate
    all_symbols = sorted(set(df["symbol"].unique()) | set(candidate_symbols))
    for sym in all_symbols:
        cand = _candidate_for(sym, candidate)
        cur = (active.get("symbols") or {}).get(sym)
        rows.append({
            "Symbol": sym,
            "Current": (f"{cur['timeframe']} · {cur['setup']}" if cur else "—"),
            "Candidate": (f"{cand['timeframe']} · {cand['setup']}" if cand
                          else "(failed gate)"),
            "WFE": (cand or {}).get("metadata", {}).get("wfe", "—"),
            "OOS PnL": (cand or {}).get("metadata", {}).get("total_oos_pnl", "—"),
        })
    table = pd.DataFrame(rows)
    st.dataframe(table, use_container_width=True, hide_index=True)

    sym = st.selectbox("Inspect symbol", options=[""] + all_symbols, index=0)
    if not sym:
        return

    cand = _candidate_for(sym, candidate)
    cur = (active.get("symbols") or {}).get(sym)

    cols = st.columns([3, 3, 2])
    cols[0].markdown("**Currently active**")
    cols[0].text(_format_params((cur or {}).get("setup_params", {}))
                 if cur else "(none)")
    cols[1].markdown("**Candidate (this run)**")
    cols[1].text(_format_params((cand or {}).get("setup_params", {}))
                 if cand else "(failed gate)")

    if cand is not None:
        if cols[2].button("Approve", key=f"approve_{sym}"):
            try:
                approve_symbol(active_path=active_path, audit_path=audit_path,
                               symbol=sym, candidate=cand, run_id=run_id)
                st.success(f"Approved {sym}.")
                st.rerun()
            except GateFailedApproveError as e:
                st.error(str(e))
        cols[2].button("Reject", key=f"reject_{sym}")  # non-sticky

    # Charts: pick the (timeframe, setup) for the candidate (or first available).
    tf, setup = (
        (cand["timeframe"], cand["setup"]) if cand
        else (df[df["symbol"] == sym].iloc[0]["timeframe"],
              df[df["symbol"] == sym].iloc[0]["setup"])
    )

    eq = walk_oos_curve(df, sym, tf, setup)
    sb = walk_oos_sharpe_bars(df, sym, tf, setup)
    sc = is_vs_oos_scatter(df, sym, tf, setup)
    axes = pick_heatmap_axes(setup)
    hm = param_heatmap(df, sym, tf, setup, axes=axes)

    st.plotly_chart(build_equity_fig(eq, sym), use_container_width=True)
    st.plotly_chart(build_sharpe_bars_fig(sb, sym), use_container_width=True)
    st.plotly_chart(build_is_oos_scatter_fig(sc, sym), use_container_width=True)
    st.plotly_chart(build_heatmap_fig(hm, sym, axes), use_container_width=True)

    summary_md = run_dir / "summary.md"
    if summary_md.exists():
        with st.expander("Run summary (summary.md)"):
            st.markdown(summary_md.read_text())
