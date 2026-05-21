"""WFO chart helpers: pure data prep + Plotly figure builders.

Data-prep functions take the full results.parquet DataFrame and return
chart-ready DataFrames. Figure builders (build_*_fig) wrap them in Plotly.
"""
from __future__ import annotations
import json

import pandas as pd

_HEATMAP_AXES = {
    "price_discovery":  ("atr_mult_stop", "target_R"),
    "vwap_bounce":      ("atr_mult_stop", "target_R"),
    "fade_extreme":     ("atr_mult_stop", "max_hold_bars"),
    "return_to_value":  ("atr_mult_stop", "arm_window_bars"),
}


def pick_heatmap_axes(setup: str) -> tuple[str, str]:
    return _HEATMAP_AXES.get(setup, ("atr_mult_stop", "target_R"))


def _filter(df: pd.DataFrame, symbol: str, timeframe: str, setup: str) -> pd.DataFrame:
    return df[(df["symbol"] == symbol)
              & (df["timeframe"] == timeframe)
              & (df["setup"] == setup)
              & (df["status"] == "ok")]


def _is_winners(df: pd.DataFrame) -> pd.DataFrame:
    """Per walk_idx, return the row with max is_sharpe."""
    if df.empty:
        return df
    idx = df.groupby("walk_idx")["is_sharpe"].idxmax()
    return df.loc[idx].sort_values("walk_idx").reset_index(drop=True)


def walk_oos_curve(df: pd.DataFrame, symbol: str, timeframe: str,
                   setup: str) -> pd.DataFrame:
    sub = _is_winners(_filter(df, symbol, timeframe, setup))
    out_cols = ["walk_idx", "oos_pnl", "cumulative_oos_pnl"]
    if sub.empty:
        return pd.DataFrame(columns=out_cols)
    out = sub[["walk_idx", "oos_pnl"]].copy()
    out["cumulative_oos_pnl"] = out["oos_pnl"].cumsum()
    return out.reset_index(drop=True)


def walk_oos_sharpe_bars(df: pd.DataFrame, symbol: str, timeframe: str,
                         setup: str) -> pd.DataFrame:
    sub = _is_winners(_filter(df, symbol, timeframe, setup))
    out_cols = ["walk_idx", "oos_sharpe"]
    if sub.empty:
        return pd.DataFrame(columns=out_cols)
    return sub[out_cols].reset_index(drop=True)


def is_vs_oos_scatter(df: pd.DataFrame, symbol: str, timeframe: str,
                      setup: str) -> pd.DataFrame:
    sub = _is_winners(_filter(df, symbol, timeframe, setup))
    out_cols = ["walk_idx", "is_sharpe", "oos_sharpe"]
    if sub.empty:
        return pd.DataFrame(columns=out_cols)
    return sub[out_cols].reset_index(drop=True)


def param_heatmap(df: pd.DataFrame, symbol: str, timeframe: str, setup: str,
                  axes: tuple[str, str]) -> pd.DataFrame:
    sub = _filter(df, symbol, timeframe, setup)
    out_cols = [axes[0], axes[1], "mean_oos_sharpe"]
    if sub.empty:
        return pd.DataFrame(columns=out_cols)
    parsed = sub["combo_values_json"].apply(json.loads)
    df2 = sub.assign(**{axes[0]: parsed.apply(lambda d: d.get(axes[0])),
                        axes[1]: parsed.apply(lambda d: d.get(axes[1]))})
    df2 = df2.dropna(subset=[axes[0], axes[1]])
    grouped = (df2.groupby([axes[0], axes[1]])["oos_sharpe"]
                  .mean()
                  .reset_index()
                  .rename(columns={"oos_sharpe": "mean_oos_sharpe"}))
    return grouped


# --- Plotly figure builders -------------------------------------------------

def build_equity_fig(df: pd.DataFrame, symbol: str):
    import plotly.graph_objects as go
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=df["walk_idx"], y=df["cumulative_oos_pnl"],
                             mode="lines+markers", name="Cumulative OOS P&L"))
    fig.update_layout(title=f"{symbol} — OOS equity (walk-stitched)",
                      xaxis_title="walk", yaxis_title="cumulative OOS P&L")
    return fig


def build_sharpe_bars_fig(df: pd.DataFrame, symbol: str):
    import plotly.graph_objects as go
    fig = go.Figure()
    fig.add_trace(go.Bar(x=df["walk_idx"], y=df["oos_sharpe"],
                         name="OOS Sharpe"))
    fig.update_layout(title=f"{symbol} — per-walk OOS Sharpe",
                      xaxis_title="walk", yaxis_title="OOS Sharpe")
    return fig


def build_is_oos_scatter_fig(df: pd.DataFrame, symbol: str):
    import plotly.graph_objects as go
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=df["is_sharpe"], y=df["oos_sharpe"],
                             mode="markers", text=df["walk_idx"], name="walks"))
    if not df.empty:
        lo = float(min(df["is_sharpe"].min(), df["oos_sharpe"].min()))
        hi = float(max(df["is_sharpe"].max(), df["oos_sharpe"].max()))
        fig.add_trace(go.Scatter(x=[lo, hi], y=[lo, hi], mode="lines",
                                 name="y=x", line=dict(dash="dash")))
    fig.update_layout(title=f"{symbol} — IS vs OOS Sharpe",
                      xaxis_title="IS Sharpe", yaxis_title="OOS Sharpe")
    return fig


def build_heatmap_fig(df: pd.DataFrame, symbol: str, axes: tuple[str, str]):
    import plotly.graph_objects as go
    if df.empty:
        return go.Figure().update_layout(title=f"{symbol} — no data")
    pivot = df.pivot(index=axes[1], columns=axes[0], values="mean_oos_sharpe")
    fig = go.Figure(data=go.Heatmap(
        z=pivot.values, x=pivot.columns, y=pivot.index,
        colorbar=dict(title="mean OOS Sharpe")))
    fig.update_layout(title=f"{symbol} — param heatmap ({axes[0]} × {axes[1]})",
                      xaxis_title=axes[0], yaxis_title=axes[1])
    return fig
