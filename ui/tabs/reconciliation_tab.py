"""Reconciliation tab — read-only view of strikes, events, and heartbeat.

All resolution actions go through scripts/reconcile_resolve.py for
auditability. This tab is for visibility only.
"""
from __future__ import annotations

import streamlit as st

from ui.data import reconciliation_repo as repo


def _heartbeat_color(age_s: float | None) -> str:
    if age_s is None:
        return "gray"
    if age_s <= 60:
        return "green"
    if age_s <= 300:
        return "orange"
    return "red"


def _heartbeat_label(age_s: float | None) -> str:
    if age_s is None:
        return "no heartbeat yet"
    if age_s < 60:
        return f"{int(age_s)}s ago"
    minutes = int(age_s // 60)
    return f"{minutes}m ago"


def _resolve_hint(direction: str) -> str:
    if direction == "mysql_only":
        return (
            "scripts/reconcile_resolve.py close <id> "
            "--exit-px <px> --setup <name> --note <text>\n"
            "  or: force-zero <id> --setup <name> --note <text>"
        )
    if direction == "broker_only":
        return (
            "scripts/reconcile_resolve.py adopt <id> "
            "--strategy <name> --setup <name> --side <long|short> "
            "--qty <q> --entry-px <p> --asset-class <equity|crypto> --note <text>"
        )
    return (
        "scripts/reconcile_resolve.py dismiss <id> --note <text>\n"
        "  (qty_drift can only be operator-resolved manually — "
        "investigate the drift first)"
    )


def render() -> None:
    st.header("Reconciliation")

    # ── Heartbeat banner ──────────────────────────────────────────────
    hb = repo.get_heartbeat_freshness()
    color = _heartbeat_color(hb["age_seconds"])
    label = _heartbeat_label(hb["age_seconds"])
    last_at = hb["last_seen_at"].isoformat() if hb["last_seen_at"] else "—"
    st.markdown(
        f"""<div style="padding: 8px 12px; border-radius: 4px;
            background-color: {color}; color: white; font-weight: 600;">
        Reconciler heartbeat: {label} (last_seen_at={last_at})
        </div>""",
        unsafe_allow_html=True,
    )

    # ── Unresolved strikes ────────────────────────────────────────────
    st.subheader("Unresolved strikes")
    strikes = repo.get_unresolved_strikes()
    if strikes.empty:
        st.success("No unresolved strikes.")
    else:
        st.dataframe(
            strikes[[
                "id", "direction", "symbol", "strategy",
                "strike_count", "last_seen_at", "last_observed_state",
            ]],
            use_container_width=True,
        )
        with st.expander("How to resolve a strike"):
            st.markdown(
                "Every action goes through the operator CLI for audit-trail "
                "reasons. Connect to the trader container:"
            )
            st.code(
                "docker compose exec trader python scripts/reconcile_resolve.py list",
                language="bash",
            )
            st.markdown("**Per-direction commands:**")
            for direction in ("mysql_only", "broker_only", "qty_drift"):
                st.markdown(f"**`{direction}`:**")
                st.code(_resolve_hint(direction), language="bash")

    # ── Recent events ─────────────────────────────────────────────────
    st.subheader("Recent events")
    events = repo.get_recent_events(limit=100)
    if events.empty:
        st.info("No events yet.")
        return
    type_options = ["(all)"] + sorted(events["type"].dropna().unique().tolist())
    selected_type = st.selectbox(
        "Filter by type", type_options, key="reconcile_event_type",
    )
    df = events if selected_type == "(all)" else events[events["type"] == selected_type]
    st.dataframe(
        df[["created_at", "type", "strategy", "symbol", "payload"]],
        use_container_width=True,
    )
