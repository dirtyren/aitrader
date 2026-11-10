"""Read-only Streamlit dashboard: Measurement / Live (test_mode) / Recorder.

Run: streamlit run arbitrage/ui/app.py

Reads files only (report JSON, live_state.json, recorder parquet dir). It never
writes, calls ccxt, places orders, or changes test_mode. The loaders are pure so
they are testable without the Streamlit runtime; streamlit is imported lazily.
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

DEFAULT_REPORT = "arbitrage/reports/report.json"
DEFAULT_STATE = "arbitrage/live_state.json"
DEFAULT_RECORDER_DIR = "arbitrage/data/recorder"


def _load_json(path) -> dict | None:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def load_report(path) -> dict | None:
    """Parsed JSON report from build_report, or None if missing/unreadable."""
    return _load_json(path)


def load_live_state(path) -> dict | None:
    """Parsed live_state.json written by the sim, or None if missing/unreadable."""
    return _load_json(path)


def report_rows(report: dict) -> pd.DataFrame:
    """Flatten the report into one row per pair/slippage/leg_risk cell."""
    rows = []
    for pair, info in report.items():
        for slip, sdata in info.get("slippage_bps", {}).items():
            for lr, st in sdata.get("leg_risk", {}).items():
                rows.append({
                    "pair": pair, "venue_buy": info.get("venue_buy"),
                    "venue_sell": info.get("venue_sell"),
                    "slippage_bps": float(slip), "leg_risk": float(lr),
                    "median_bps": st.get("median_bps"),
                    "pct_positive": st.get("pct_positive"),
                    "persistence_median_ms": st.get("persistence_median_ms"),
                })
    return pd.DataFrame(rows)


def recorder_coverage(out_dir) -> pd.DataFrame:
    """Per venue/symbol capture coverage from <out_dir>/<venue>/<SYMBOL>.parquet."""
    cols = ["venue", "symbol", "rows", "first_ts_utc_ms", "last_ts_utc_ms", "span_s"]
    rows = []
    for p in sorted(Path(out_dir).glob("*/*.parquet")):
        try:
            ts = pd.read_parquet(p, columns=["ts_utc_ms"])["ts_utc_ms"]
        except Exception:  # unreadable/partially written file: skip, read-only view
            continue
        if ts.empty:
            continue
        first, last = int(ts.min()), int(ts.max())
        rows.append({"venue": p.parent.name, "symbol": p.stem, "rows": len(ts),
                     "first_ts_utc_ms": first, "last_ts_utc_ms": last,
                     "span_s": (last - first) / 1000.0})
    return pd.DataFrame(rows, columns=cols)


def _measurement_tab(st, path: str) -> None:
    report = load_report(path)
    if report is None:
        st.info(f"No report at {path}")
        return
    df = report_rows(report)
    st.subheader("Cells")
    st.dataframe(df, use_container_width=True)
    if not df.empty:
        st.subheader("Best cell per pair (median net bps)")
        best = df.loc[df.groupby("pair")["median_bps"].idxmax()]
        st.dataframe(best, use_container_width=True)


def _live_tab(st, path: str) -> None:
    state = load_live_state(path)
    if state is None:
        st.info(f"No live state at {path}")
        return
    if state.get("test_mode"):
        st.success("test_mode: ON (paper only)")
    else:
        st.error("test_mode: OFF")
    c1, c2, c3 = st.columns(3)
    c1.metric("Spread (bps)", state.get("spread_bps"))
    c2.metric("Realized P&L (USD)", state.get("pnl_usd"))
    c3.metric("Round trips", state.get("round_trips"))
    resources = state.get("resources") or {}
    if resources:
        st.subheader("Resource meter")
        st.dataframe(pd.DataFrame(resources).T, use_container_width=True)


def _recorder_tab(st, out_dir: str) -> None:
    cov = recorder_coverage(out_dir)
    if cov.empty:
        st.info(f"No recorder data in {out_dir}")
        return
    st.dataframe(cov, use_container_width=True)
    st.bar_chart(cov.groupby("venue")["rows"].sum())


def main() -> None:
    import streamlit as st

    st.set_page_config(page_title="Arbitrage edge", layout="wide")
    st.title("Arbitrage edge measurement (read-only)")
    report_path = st.sidebar.text_input("Report JSON", DEFAULT_REPORT)
    state_path = st.sidebar.text_input("Live state JSON", DEFAULT_STATE)
    rec_dir = st.sidebar.text_input("Recorder dir", DEFAULT_RECORDER_DIR)
    if st.sidebar.button("Refresh"):
        st.rerun()
    t_meas, t_live, t_rec = st.tabs(["Measurement", "Live (test_mode)", "Recorder"])
    with t_meas:
        _measurement_tab(st, report_path)
    with t_live:
        _live_tab(st, state_path)
    with t_rec:
        _recorder_tab(st, rec_dir)


if __name__ == "__main__":
    main()
