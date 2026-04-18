"""Streamlit real-time dashboard for regime_trader."""

import json
import os

import plotly.graph_objects as go
import streamlit as st
from streamlit_autorefresh import st_autorefresh

_STATE_FILE = os.environ.get("STATE_FILE_PATH", "runtime/trading_state.json")


# ---------------------------------------------------------------------------
# State loading
# ---------------------------------------------------------------------------

def load_state() -> dict:
    """Read trading_state.json. Path overrideable via STATE_FILE_PATH env var."""
    try:
        with open(_STATE_FILE, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {
            "regime": "Unknown",
            "confidence": 0.0,
            "stable": False,
            "n_regimes": 0,
            "circuit_level": 0,
            "drawdown": 0.0,
            "leverage": 1.0,
            "trading_suspended": False,
            "signals": [],
            "total_equity": 0.0,
            "daily_pnl": 0.0,
            "prices": [],
            "regimes": [],
            "volumes": [],
            "portfolio": [],
        }


# ---------------------------------------------------------------------------
# Panel helpers
# ---------------------------------------------------------------------------

_REGIME_COLORS = {
    "Crash": "red",
    "Bear": "orange",
    "Neutral": "gray",
    "Bull": "green",
    "Euphoria": "purple",
}

_CIRCUIT_COLORS = {0: "green", 1: "yellow", 2: "orange", 3: "red"}

_REGIME_BAND_COLORS = {
    "Crash": "rgba(255,0,0,0.15)",
    "Bear": "rgba(255,165,0,0.15)",
    "Neutral": "rgba(128,128,128,0.15)",
    "Bull": "rgba(0,128,0,0.15)",
    "Euphoria": "rgba(128,0,128,0.15)",
}


def render_regime_panel(state: dict) -> None:
    """
    Display:
    - Current regime (large colored text: Crash=red, Bear=orange, Neutral=gray, Bull=green, Euphoria=purple)
    - HMM confidence score as a progress bar (0-100%)
    - Stable/Unstable badge
    - Number of regimes detected (from state["n_regimes"])
    """
    st.subheader("Regime Status")

    regime = state.get("regime", "Unknown")
    color = _REGIME_COLORS.get(regime, "white")
    st.markdown(
        f"<h2 style='color:{color}'>{regime}</h2>",
        unsafe_allow_html=True,
    )

    confidence = float(state.get("confidence", 0.0))
    st.progress(min(max(confidence, 0.0), 1.0), text=f"HMM Confidence: {confidence * 100:.1f}%")

    stable = state.get("stable", False)
    if stable:
        st.success("STABLE")
    else:
        st.warning("UNSTABLE")

    n_regimes = state.get("n_regimes", 0)
    st.metric("Regimes Detected", n_regimes)


def render_risk_panel(state: dict) -> None:
    """
    Display:
    - Circuit breaker level (0-3) with colored indicator
    - Current drawdown % (negative number, red if < -5%)
    - Current leverage multiplier
    - Trading suspended badge (red banner if True)
    """
    st.subheader("Risk Status")

    circuit_level = int(state.get("circuit_level", 0))
    circuit_color = _CIRCUIT_COLORS.get(circuit_level, "gray")
    st.markdown(
        f"<span style='color:{circuit_color}'>&#11044;</span> Circuit Breaker Level: **{circuit_level}**",
        unsafe_allow_html=True,
    )

    drawdown = float(state.get("drawdown", 0.0))
    drawdown_pct = drawdown * 100
    if drawdown_pct < -5.0:
        st.markdown(
            f"<p style='color:red'>Drawdown: {drawdown_pct:.2f}%</p>",
            unsafe_allow_html=True,
        )
    else:
        st.metric("Drawdown", f"{drawdown_pct:.2f}%")

    leverage = float(state.get("leverage", 1.0))
    st.metric("Leverage Multiplier", f"{leverage:.2f}x")

    trading_suspended = state.get("trading_suspended", False)
    if trading_suspended:
        st.error("TRADING SUSPENDED")


def render_signal_panel(state: dict) -> None:
    """
    Display:
    - Table of recent signals: timestamp, ticker, action (BUY/SELL/HOLD), allocation_pct, regime, confidence
    - Real-time P&L: total equity, daily P&L (green if positive, red if negative)
    """
    st.subheader("Signal Feed")

    signals = state.get("signals", [])
    if signals:
        import pandas as pd  # local import to avoid hard dep at module level

        columns = ["timestamp", "ticker", "action", "allocation_pct", "regime", "confidence"]
        rows = []
        for sig in signals:
            rows.append({col: sig.get(col, "") for col in columns})
        df = pd.DataFrame(rows, columns=columns)
        st.dataframe(df, use_container_width=True)
    else:
        st.info("No signals yet.")

    total_equity = float(state.get("total_equity", 0.0))
    daily_pnl = float(state.get("daily_pnl", 0.0))

    st.metric("Total Equity", f"${total_equity:,.2f}")

    if daily_pnl >= 0:
        st.markdown(
            f"<p style='color:green'>Daily P&L: +${daily_pnl:,.2f}</p>",
            unsafe_allow_html=True,
        )
    else:
        st.markdown(
            f"<p style='color:red'>Daily P&L: -${abs(daily_pnl):,.2f}</p>",
            unsafe_allow_html=True,
        )


def render_chart_panel(state: dict) -> None:
    """
    Display:
    - Price chart with regime color-coded background bands
      (each bar colored by its detected regime using plotly go.Bar or scatter)
    - Volume distribution as histogram
    """
    st.subheader("Distribution Charts")

    prices = state.get("prices", [])
    regimes = state.get("regimes", [])
    volumes = state.get("volumes", [])

    # Price chart with regime-colored background bands
    if prices:
        fig_price = go.Figure()

        # Determine x-axis (indices or provided timestamps)
        x = list(range(len(prices)))

        # Add regime-colored background shapes
        if regimes:
            # Group consecutive same-regime segments for band shading
            i = 0
            n = len(regimes)
            while i < n:
                current_regime = regimes[i]
                j = i
                while j < n and regimes[j] == current_regime:
                    j += 1
                band_color = _REGIME_BAND_COLORS.get(current_regime, "rgba(200,200,200,0.1)")
                fig_price.add_vrect(
                    x0=i,
                    x1=j - 1,
                    fillcolor=band_color,
                    opacity=1.0,
                    layer="below",
                    line_width=0,
                )
                i = j

        # Price line
        fig_price.add_trace(
            go.Scatter(
                x=x,
                y=prices,
                mode="lines",
                name="Price",
                line=dict(color="steelblue", width=1.5),
            )
        )
        fig_price.update_layout(
            title="Price with Regime Bands",
            xaxis_title="Bar Index",
            yaxis_title="Price",
            height=300,
            margin=dict(l=40, r=20, t=40, b=30),
        )
        st.plotly_chart(fig_price, use_container_width=True)
    else:
        st.info("No price data available.")

    # Volume histogram
    if volumes:
        fig_vol = go.Figure()
        fig_vol.add_trace(
            go.Histogram(
                x=volumes,
                name="Volume",
                marker_color="steelblue",
                opacity=0.75,
            )
        )
        fig_vol.update_layout(
            title="Volume Distribution",
            xaxis_title="Volume",
            yaxis_title="Count",
            height=250,
            margin=dict(l=40, r=20, t=40, b=30),
        )
        st.plotly_chart(fig_vol, use_container_width=True)
    else:
        st.info("No volume data available.")


def render_portfolio_panel(state: dict):
    st.subheader("Portfolio")
    assets = state.get("portfolio", [])
    if not assets:
        st.info("No portfolio data available.")
        return

    # Allocation pie chart: current vs target
    import plotly.graph_objects as go
    tickers = [a["ticker"] for a in assets]
    current_w = [a.get("current_weight", 0.0) for a in assets]
    target_w = [a.get("target_weight", 0.0) for a in assets]

    fig = go.Figure()
    fig.add_trace(go.Bar(name="Current", x=tickers, y=current_w))
    fig.add_trace(go.Bar(name="Target", x=tickers, y=target_w))
    fig.update_layout(barmode="group", height=250, margin=dict(t=20, b=20),
                      yaxis_tickformat=".0%")
    st.plotly_chart(fig, use_container_width=True)

    # Per-asset regime table
    rows = [{"Ticker": a["ticker"], "Regime": a.get("regime", "—"),
             "Conf": f"{a.get('confidence', 0):.0%}",
             "Drift": f"{a.get('drift', 0):+.1%}"} for a in assets]
    st.dataframe(rows, use_container_width=True)


# ---------------------------------------------------------------------------
# Main app
# ---------------------------------------------------------------------------

def main() -> None:
    st.set_page_config(page_title="regime_trader", layout="wide")
    st.title("Regime Trader — Live Dashboard")

    # Non-blocking auto-refresh every 5 seconds
    st_autorefresh(interval=5000, key="dashboard_refresh")

    # Load state
    state = load_state()

    # Layout: 2x2 grid using st.columns
    col1, col2 = st.columns(2)
    with col1:
        render_regime_panel(state)
        render_signal_panel(state)
        render_portfolio_panel(state)
    with col2:
        render_risk_panel(state)
        render_chart_panel(state)


if __name__ == "__main__":
    main()
