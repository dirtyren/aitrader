"""KPI row builder + PnL formatter helpers."""
from __future__ import annotations

from typing import Optional


def format_pnl(value: Optional[float], *, prefix: str = "$") -> str:
    """Return a colored markdown string for a PnL number.

    None → '—' in neutral grey.
    Positive → emerald, Negative → red.
    """
    if value is None:
        return '<span class="pnl-neu">—</span>'
    cls = "pnl-pos" if value > 0 else ("pnl-neg" if value < 0 else "pnl-neu")
    sign = "+" if value > 0 else ""
    return f'<span class="{cls}">{sign}{prefix}{value:,.2f}</span>'


def format_pnl_inline(value: Optional[float], *, fmt: str = "{:+.2f}") -> str:
    """Like `format_pnl` but renders the raw formatted number (no $ prefix)
    inside a colored monospace span. Use inside table cells where you want
    the value to look like a code-span but with red/green coloring.

    None or NaN renders as a neutral em-dash.
    Zero is treated as neutral (neither positive nor negative).
    """
    import math

    if value is None or (isinstance(value, float) and math.isnan(value)):
        return '<span class="pnl-neu" style="font-family:monospace">—</span>'
    if value > 0:
        cls = "pnl-pos"
    elif value < 0:
        cls = "pnl-neg"
    else:
        cls = "pnl-neu"
    return f'<span class="{cls}" style="font-family:monospace">{fmt.format(value)}</span>'


def format_pct(value: Optional[float]) -> str:
    if value is None:
        return "—"
    return f"{value * 100:.1f}%"


def render_kpi_row(kpis) -> None:
    """Render a 4×2 grid of metric tiles for a `KPIs` dataclass."""
    import streamlit as st

    row1 = st.columns(4)
    row1[0].metric("Total PnL", _money(kpis.total_pnl))
    row1[1].metric("Trades", str(kpis.trade_count))
    row1[2].metric("Win Rate", format_pct(kpis.win_rate))
    row1[3].metric("Profit Factor", format_num(kpis.profit_factor, fmt="{:.2f}"))

    row2 = st.columns(4)
    row2[0].metric("Avg Win", _money(kpis.avg_win))
    row2[1].metric("Avg Loss", _money(kpis.avg_loss))
    row2[2].metric("Expectancy R", format_num(kpis.expectancy_R, fmt="{:.2f}"))
    row2[3].metric("Max DD", _money(kpis.max_drawdown))

    row3 = st.columns(4)
    row3[0].metric("Sharpe", format_num(kpis.sharpe, fmt="{:.2f}"))
    row3[1].metric("Avg Bars", format_num(kpis.avg_bars_held, fmt="{:.1f}"))
    row3[2].metric("Best Trade", _money(kpis.best_trade))
    row3[3].metric("Worst Trade", _money(kpis.worst_trade))


def _money(v: Optional[float]) -> str:
    if v is None:
        return "—"
    sign = "-" if v < 0 else ""
    return f"{sign}${abs(v):,.2f}"


def format_num(v: Optional[float], *, fmt: str) -> str:
    return "—" if v is None else fmt.format(v)
