"""Strategies tab — landing page (cards per strategy) + detail drill-down."""
from __future__ import annotations

from datetime import datetime

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from ui.components import period_selector, strategy_card
from ui.components.kpi_row import render_kpi_row
from ui.data import stats, trades_repo


def render() -> None:
    start, end = period_selector.render()
    st.session_state["period"] = (start, end)

    try:
        all_strategies = trades_repo.list_strategies()
    except Exception as e:
        st.error(f"MySQL unreachable: {e}")
        st.stop()
        return

    if not all_strategies:
        st.info("No strategies registered yet.")
        return

    selected: str | None = st.session_state.get("selected_strategy")

    if selected is None:
        _render_landing(all_strategies, start, end)
    else:
        if st.button("← Back to all strategies"):
            st.session_state["selected_strategy"] = None
            st.rerun()
        _render_detail(selected, start, end)


def _render_landing(strategies: list[str], start: datetime, end: datetime) -> None:
    st.subheader("Strategies")
    st.caption(f"Period: {start.date()} → {end.date()}")
    cols = st.columns(2)
    for i, name in enumerate(strategies):
        df = trades_repo.get_closed_trades(name, start, end)
        kpis = stats.compute_kpis(df)
        with cols[i % 2]:
            if strategy_card.render(name, kpis):
                st.session_state["selected_strategy"] = name
                st.rerun()


def _render_detail(strategy: str, start: datetime, end: datetime) -> None:
    st.subheader(f"Strategy — {strategy}")
    st.caption(f"Period: {start.date()} → {end.date()}")

    df = trades_repo.get_closed_trades(strategy, start, end)
    kpis = stats.compute_kpis(df)
    render_kpi_row(kpis)

    if df.empty:
        st.info("No trades in this period.")
        return

    _render_charts(df)
    _render_trades_table(df)


def _render_charts(df: pd.DataFrame) -> None:
    eq = stats.equity_curve(df)
    dp = stats.daily_pnl(df)
    rs = stats.r_distribution(df)
    wl = stats.winloss_by_setup(df)

    c1, c2 = st.columns(2)
    with c1:
        st.markdown("**Equity Curve**")
        fig = px.line(eq, x="closed_at", y="cum_pnl")
        fig.update_layout(height=300, margin=dict(l=10, r=10, t=10, b=10),
                          paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)")
        st.plotly_chart(fig, use_container_width=True)

    with c2:
        st.markdown("**Daily P&L**")
        colors = ["#10b981" if v >= 0 else "#ef4444" for v in dp["pnl"]]
        fig = go.Figure(data=[go.Bar(x=dp["day"], y=dp["pnl"], marker_color=colors)])
        fig.update_layout(height=300, margin=dict(l=10, r=10, t=10, b=10),
                          paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)")
        st.plotly_chart(fig, use_container_width=True)

    c3, c4 = st.columns(2)
    with c3:
        st.markdown("**R-Distribution**")
        fig = px.histogram(rs, nbins=20)
        fig.update_layout(height=300, margin=dict(l=10, r=10, t=10, b=10), showlegend=False,
                          paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)")
        st.plotly_chart(fig, use_container_width=True)

    with c4:
        st.markdown("**Wins / Losses by Setup**")
        if wl.empty:
            st.caption("No data.")
        else:
            fig = go.Figure(data=[
                go.Bar(name="Wins", x=wl["setup_name"], y=wl["wins"], marker_color="#10b981"),
                go.Bar(name="Losses", x=wl["setup_name"], y=wl["losses"], marker_color="#ef4444"),
            ])
            fig.update_layout(barmode="group", height=300, margin=dict(l=10, r=10, t=10, b=10),
                              paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)")
            st.plotly_chart(fig, use_container_width=True)


def _render_trades_table(df: pd.DataFrame) -> None:
    st.markdown("### Trades")
    q = st.text_input("Filter (symbol or setup)", value="").strip().lower()
    show = df.copy()
    if q:
        mask = (
            show["symbol"].str.lower().str.contains(q, na=False) |
            show["setup_name"].str.lower().str.contains(q, na=False)
        )
        show = show[mask]
    st.dataframe(show, use_container_width=True, hide_index=True)
