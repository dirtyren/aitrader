"""Live Trading tab — open positions across all strategies."""
from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
import streamlit as st

from ui.data.positions_repo import get_open
from ui.data.state_files import get_last_price


def _enrich(df: pd.DataFrame) -> pd.DataFrame:
    """Add Current Px, Unrealized PnL, R-so-far, Age to a positions df."""
    if df.empty:
        return df.assign(current_px=[], unrealized=[], R_so_far=[], age=[])

    now = datetime.now(timezone.utc)
    enriched = df.copy()
    last = enriched.apply(lambda r: get_last_price(r["strategy"], r["symbol"]), axis=1)
    enriched["current_px"] = last

    def unrealized(row):
        if row["current_px"] is None:
            return None
        side_sign = 1 if row["side"] == "long" else -1
        return float((row["current_px"] - float(row["entry_px"])) * float(row["qty"]) * side_sign)

    def r_so_far(row):
        if row["current_px"] is None or row["initial_stop_px"] is None:
            return None
        risk = abs(float(row["entry_px"]) - float(row["initial_stop_px"]))
        if risk == 0:
            return None
        side_sign = 1 if row["side"] == "long" else -1
        return float((row["current_px"] - float(row["entry_px"])) * side_sign / risk)

    enriched["unrealized"] = enriched.apply(unrealized, axis=1)
    enriched["R_so_far"] = enriched.apply(r_so_far, axis=1)
    enriched["age"] = enriched["opened_at"].apply(
        lambda t: str(now - pd.Timestamp(t).to_pydatetime()).split(".")[0]
        if t is not None else "—"
    )
    return enriched


def render() -> None:
    st.subheader("Live Trading — Open Positions")

    try:
        df = get_open()
    except Exception as e:
        st.error(f"MySQL unreachable: {e}")
        st.stop()
        return

    if df.empty:
        st.info("No open positions across any strategy.")
        return

    strategies = sorted(df["strategy"].unique().tolist())
    selected = st.multiselect("Filter strategies", options=strategies, default=strategies)
    df = df[df["strategy"].isin(selected)]

    enriched = _enrich(df)

    display_cols = [
        "strategy", "symbol", "asset_class", "setup_name", "side", "qty",
        "entry_px", "current_px", "unrealized", "R_so_far",
        "stop_px", "target_px", "age",
    ]
    show = enriched[display_cols].rename(columns={
        "strategy": "Strategy", "symbol": "Symbol", "asset_class": "Asset",
        "setup_name": "Setup", "side": "Side", "qty": "Qty",
        "entry_px": "Entry", "current_px": "Current",
        "unrealized": "Unrealized PnL", "R_so_far": "R so far",
        "stop_px": "Stop", "target_px": "Target", "age": "Age",
    })
    st.dataframe(show, use_container_width=True, hide_index=True)
