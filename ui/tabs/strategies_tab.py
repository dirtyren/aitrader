"""Strategies tab — landing page (cards per strategy) + detail drill-down."""
from __future__ import annotations

from datetime import datetime

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from broker.alpaca_client import AlpacaClient
from state.mysql_store import MySQLStore
from state.strategy_close_all import close_all_open_positions
from ui.components import period_selector, strategy_card
from ui.components.kpi_row import render_kpi_row
from ui.data import stats, strategy_admin, trades_repo
from ui.data.strategy_configs import list_yaml_strategy_names


@st.cache_resource
def _get_alpaca() -> AlpacaClient:
    return AlpacaClient()


@st.cache_resource
def _get_store_for(strategy_name: str) -> MySQLStore:
    """Per-strategy MySQLStore (so position_closed and is_strategy_enabled
    target the right strategy_id). Cached by name to avoid reconnecting."""
    s = MySQLStore(strategy_name=strategy_name)
    s.ensure_schema()
    s.upsert_strategy()
    return s


@st.cache_resource
def _get_admin_store() -> MySQLStore:
    """Read-only admin singleton for listing strategies."""
    s = MySQLStore(strategy_name="operator")
    s.ensure_schema()
    return s


def render() -> None:
    start, end = period_selector.render()
    st.session_state["period"] = (start, end)

    try:
        all_strategies = list_yaml_strategy_names()
    except Exception as e:
        st.error(f"Failed to load strategy configs: {e}")
        st.stop()
        return

    if not all_strategies:
        st.info("No strategies configured yet.")
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

    _render_admin_panel()

    st.markdown("---")
    cols = st.columns(2)
    for i, name in enumerate(strategies):
        df = trades_repo.get_closed_trades(name, start, end)
        kpis = stats.compute_kpis(df)
        with cols[i % 2]:
            if strategy_card.render(name, kpis):
                st.session_state["selected_strategy"] = name
                st.rerun()


_STATE_BADGE = {
    "enabled": ("🟢", "enabled"),
    "disabling": ("🟠", "disabling"),
    "disabled": ("🔴", "disabled"),
}


def _render_admin_panel() -> None:
    """Per-strategy kill-switch table.

    Disable: confirms with a required operator note (≥3 chars), submits
    market closes for all open positions synchronously. On any failure
    the strategy stays in `disabling` and the trader retries on its loop.
    Re-enable: single click, no modal — re-enabling cannot lose money.
    """
    st.markdown("### Strategy controls")
    try:
        admin_store = _get_admin_store()
        df = strategy_admin.get_admin_view(admin_store)
    except Exception as exc:
        st.error(f"Could not load strategy admin view: {exc}")
        return
    if df.empty:
        st.info("No strategies registered yet.")
        return

    header = st.columns([2, 1, 1, 1, 1, 1, 2])
    header[0].markdown("**Name**")
    header[1].markdown("**State**")
    header[2].markdown("**Open**")
    header[3].markdown("**Today P&L**")
    header[4].markdown("**Total P&L**")
    header[5].markdown("**Win rate**")
    header[6].markdown("**Action**")

    for _, row in df.iterrows():
        sid = int(row["id"])
        name = str(row["name"])
        state = str(row["state"])
        emoji, label = _STATE_BADGE.get(state, ("⚪", state))
        confirm_key = f"strat_confirm_{sid}"
        note_key = f"strat_note_{sid}"

        cols = st.columns([2, 1, 1, 1, 1, 1, 2])
        cols[0].markdown(f"**{name}**")
        cols[1].markdown(f"{emoji} {label}")
        cols[2].markdown(f"`{int(row['open_count'])}`")
        cols[3].markdown(f"`{row['today_pnl']:+.2f}`")
        cols[4].markdown(f"`{row['total_pnl']:+.2f}`")
        cols[5].markdown(f"`{row['win_rate']*100:.1f}%`"
                         if row["trade_count"] else "—")

        with cols[6]:
            confirming = st.session_state.get(confirm_key, False)
            if state == "enabled":
                if not confirming:
                    if st.button("Disable", key=f"strat_btn_{sid}",
                                 type="primary"):
                        st.session_state[confirm_key] = True
                        st.rerun()
                # else: confirm UI below the row
            elif state == "disabled":
                if st.button("Enable", key=f"strat_btn_{sid}"):
                    admin_store.set_strategy_state(
                        strategy_id=sid, enabled=True, state="enabled",
                        reason="operator_enable",
                    )
                    st.rerun()
            else:  # disabling
                st.caption(f"Sweeping… {int(row['open_count'])} left")

        if state == "enabled" and st.session_state.get(confirm_key):
            with st.container(border=True):
                st.warning(
                    f"Disabling **{name}** will submit market closes for "
                    f"**{int(row['open_count'])}** open position(s)."
                )
                note = st.text_input(
                    "Operator note (≥3 chars, required)",
                    key=note_key,
                    placeholder="e.g. risk review — pause until tomorrow",
                )
                c, x = st.columns([1, 1])
                disabled = len((note or "").strip()) < 3
                if c.button("Confirm disable", key=f"strat_go_{sid}",
                            type="primary", disabled=disabled):
                    _run_disable(sid, name, note)
                    st.session_state[confirm_key] = False
                    st.rerun()
                if x.button("Cancel", key=f"strat_x_{sid}"):
                    st.session_state[confirm_key] = False
                    st.rerun()


def _run_disable(strategy_id: int, strategy_name: str, note: str) -> None:
    """Set state=disabling, sweep positions, transition to disabled on
    clean sweep — otherwise leave at disabling for the trader to retry."""
    try:
        admin_store = _get_admin_store()
        strategy_store = _get_store_for(strategy_name)
        admin_store.set_strategy_state(
            strategy_id=strategy_id,
            enabled=False, state="disabling",
            reason=note,
        )
    except Exception as exc:
        st.error(f"Could not mark strategy disabling: {exc}")
        return

    try:
        with st.spinner(f"Closing positions for {strategy_name}…"):
            result = close_all_open_positions(
                alpaca=_get_alpaca(), mysql=strategy_store,
                strategy_name=strategy_name, reason="operator_disable",
            )
    except Exception as exc:
        st.error(f"Sweep failed: {exc}. Strategy left in `disabling` "
                 "state — trader will retry on its next cycle.")
        return

    if not result.failed and result.total == len(result.closed):
        admin_store.set_strategy_state(
            strategy_id=strategy_id,
            enabled=False, state="disabled",
            reason="dashboard_disable_complete",
        )
        st.success(
            f"Disabled **{strategy_name}** — closed "
            f"{len(result.closed)}/{result.total} positions."
        )
    else:
        failed_summary = ", ".join(f"{s} ({why})" for s, why in result.failed)
        st.warning(
            f"Closed {len(result.closed)}/{result.total} positions. "
            f"Failures: {failed_summary or 'none'}. "
            "Strategy left in `disabling` state — trader will retry on "
            "its next cycle."
        )


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
