"""One summary card per strategy on the Strategies landing page."""
from __future__ import annotations

from ui.components.kpi_row import format_pnl, format_pct
from ui.data.stats import KPIs


def render(strategy: str, kpis: KPIs) -> bool:
    """Render the card; return True if the user clicked it (i.e. drill in)."""
    import streamlit as st

    with st.container(border=True):
        st.markdown(f"### {strategy}")
        c1, c2, c3, c4 = st.columns(4)
        c1.markdown(f"**Total PnL**<br>{format_pnl(kpis.total_pnl)}", unsafe_allow_html=True)
        c2.markdown(f"**Win Rate**<br>{format_pct(kpis.win_rate)}", unsafe_allow_html=True)
        c3.markdown(f"**# Trades**<br>{kpis.trade_count}", unsafe_allow_html=True)
        c4.markdown(f"**Max DD**<br>{format_pnl(kpis.max_drawdown)}", unsafe_allow_html=True)
        return st.button("Open detail", key=f"open_{strategy}", use_container_width=True)
