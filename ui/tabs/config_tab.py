"""Configuration tab — read-only viewer for strategy YAML + DB membership.

Sidebar list of every discovered strategy (YAML ∪ MySQL) with a status
badge, detail pane on the right showing curated sections plus a raw
YAML expander.
"""
from __future__ import annotations

from typing import Any

import pandas as pd
import streamlit as st
import yaml

from ui.data.strategy_configs import (
    AssetClass,
    Setup,
    StrategyConfig,
    StrategyEntry,
    discover_strategies,
)


def render() -> None:
    st.subheader("Configuration — Strategy Definitions")

    entries = discover_strategies()
    if not entries:
        st.info("No strategies configured yet.")
        return

    left, right = st.columns([1, 3], gap="large")

    with left:
        labels = [_format_sidebar_label(e) for e in entries]
        idx = st.radio(
            "Strategy",
            options=list(range(len(entries))),
            format_func=lambda i: labels[i],
            key="config_selected_strategy",
        )

    selected = entries[idx]
    with right:
        if selected.status == "db-only":
            _render_db_only(selected.name)
        else:
            assert selected.config is not None
            _render_detail(selected.status, selected.config)


def _format_sidebar_label(entry: StrategyEntry) -> str:
    if entry.config is None:
        return f"{entry.name}\n[{entry.status}]"
    env = entry.config.env or "—"
    summary_parts = [
        f"{ac.name}·{len(ac.symbols)}" for ac in entry.config.asset_classes
    ]
    summary = " / ".join(summary_parts) if summary_parts else "no assets"
    return f"{entry.name}\n[{entry.status}] {env} · {summary}"


def _render_db_only(name: str) -> None:
    st.markdown(f"### {name}")
    st.caption("Status: **db-only**")
    st.info(
        "This strategy is registered in MySQL but has no current YAML config — "
        "likely renamed or retired. Trades are retained in the database but no "
        "longer shown in the Strategies/Live tabs."
    )


def _render_detail(status: str, cfg: StrategyConfig) -> None:
    _render_header(status, cfg)
    _render_assets(cfg.asset_classes)
    _render_risk(cfg.risk)
    _render_setups(cfg.setups)
    _render_filters(cfg.filters)
    _render_broker(cfg.broker)
    _render_backtest(cfg.backtest)
    _render_raw(cfg.raw)


def _render_header(status: str, cfg: StrategyConfig) -> None:
    st.markdown(f"### {cfg.name}")
    st.caption(
        f"Status: **{status}** · Version: **{cfg.version or '—'}** · "
        f"Env: **{cfg.env or '—'}** · YAML: `{cfg.yaml_path}`"
    )


def _render_assets(asset_classes: list[AssetClass]) -> None:
    st.markdown("#### Assets")
    if not asset_classes:
        st.caption("No asset classes defined.")
        return
    for ac in asset_classes:
        st.markdown(f"**{ac.name}** ({len(ac.symbols)} symbols)")
        if not ac.symbols:
            st.caption("No symbols configured.")
            continue
        session = (
            f"{ac.session_open_local or '—'}–{ac.session_close_local or '—'}"
        )
        rows = [{
            "Symbol": s,
            "Session": session,
            "Timezone": ac.timezone or "—",
            "Slippage (bps)": ac.slippage_bps,
            "Commission (bps)": ac.commission_bps,
            "Commission/share": ac.commission_per_share,
        } for s in ac.symbols]
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)


def _kv_table(d: dict[str, Any]) -> None:
    if not d:
        st.caption("Empty.")
        return
    rows = [{"Key": k, "Value": _scalar(v)} for k, v in d.items()]
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)


def _scalar(v: Any) -> Any:
    if isinstance(v, (dict, list)):
        return yaml.safe_dump(v, default_flow_style=True).strip()
    return v


def _render_risk(risk: dict[str, Any]) -> None:
    st.markdown("#### Risk")
    _kv_table(risk)


def _render_setups(setups: list[Setup]) -> None:
    st.markdown("#### Setups")
    if not setups:
        st.caption("No setups defined.")
        return
    sorted_setups = sorted(setups, key=lambda s: (not s.enabled, s.name))
    rows = [{
        "Setup": s.name,
        "Enabled": "✓" if s.enabled else "✗",
        "Parameters": yaml.safe_dump(s.params, default_flow_style=True).strip() if s.params else "—",
    } for s in sorted_setups]
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)


def _render_filters(filters: dict[str, Any]) -> None:
    st.markdown("#### Filters")
    _kv_table(filters)


def _render_broker(broker: dict[str, Any]) -> None:
    st.markdown("#### Broker")
    _kv_table(broker)


def _render_backtest(backtest: dict[str, Any]) -> None:
    st.markdown("#### Backtest")
    _kv_table(backtest)


def _render_raw(raw: dict) -> None:
    with st.expander("Raw YAML", expanded=False):
        st.code(yaml.safe_dump(raw, sort_keys=False), language="yaml")
