"""Live Trading tab — open positions across all strategies."""
from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
import streamlit as st

from ui.data.positions_repo import get_open
from ui.data.state_files import get_last_price
from ui.data.strategy_configs import list_yaml_strategy_names


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

    opened = pd.to_datetime(enriched["opened_at"], errors="coerce", utc=True)
    age_delta = pd.Timestamp(now) - opened
    enriched["age"] = age_delta.apply(
        lambda d: "—" if pd.isna(d) else str(d).split(".")[0]
    )
    return enriched


def render() -> None:
    st.subheader("Live Trading — Open Positions")
    _live_body()


@st.fragment(run_every="5s")
def _live_body() -> None:
    """Re-runs every 5s in isolation — does NOT trigger a full-page rerun,
    so Settings forms, Strategy admin buttons, and other tabs stay stable."""
    try:
        df = get_open()
        yaml_strategies = list_yaml_strategy_names()
    except Exception as e:
        st.error(f"MySQL unreachable: {e}")
        return

    open_strategies = sorted(df["strategy"].unique().tolist()) if not df.empty else []
    options = sorted(set(yaml_strategies) | set(open_strategies))

    if not options:
        st.info("No strategies registered yet.")
        return

    selected = st.multiselect(
        "Filter strategies",
        options=options,
        default=options,
        key="live_strategy_filter",
    )

    if df.empty:
        st.info("No open positions across any strategy.")
        return

    df = df[df["strategy"].isin(selected)]
    if df.empty:
        st.info("No open positions for the selected strategies.")
        return

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
    styled = show.style.map(_pnl_color, subset=["Unrealized PnL"])
    st.dataframe(styled, use_container_width=True, hide_index=True)


def _pnl_color(value) -> str:
    """Tailwind-ish red for negative PnL, blue for positive, neutral otherwise.

    Streamlit's default theme is dark, so we pick saturated foreground colors
    rather than backgrounds — matches the existing financial-dashboard look.
    """
    if value is None or pd.isna(value):
        return ""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return ""
    if v > 0:
        return "color: #3b82f6; font-weight: 600"  # blue-500
    if v < 0:
        return "color: #ef4444; font-weight: 600"  # red-500
    return ""
