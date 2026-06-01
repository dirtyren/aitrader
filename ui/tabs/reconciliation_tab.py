"""Reconciliation tab — visibility plus scoped operator action.

Visibility for all strikes/events. Operator action is scoped to
`broker_only` strikes only — that's the one direction the dashboard can
safely flatten without altering managed-position state. All other
resolutions still go through scripts/reconcile_resolve.py for auditability.

Every dashboard close writes:
  - an `operator_action` event row (via MySQLStore.resolve_strike), AND
  - a runtime/operator_close_audit_<timestamp>.jsonl record.
"""
from __future__ import annotations

import os

import streamlit as st

from broker.alpaca_client import AlpacaClient
from broker.alpaca_router import AlpacaRouter
from state.mysql_store import MySQLStore
from state.operator_close import close_broker_only_strike
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


@st.cache_resource
def _get_alpaca() -> AlpacaRouter:
    # Reconciliation tab spans both asset classes; the router fans reads
    # across equity + crypto and routes writes by symbol shape. close_broker_only
    # only needs submit_order/cancel_order/list_orders/get_positions, all of
    # which the router proxies with the same shape as AlpacaClient.
    return AlpacaRouter()


@st.cache_resource
def _get_store() -> MySQLStore:
    s = MySQLStore(strategy_name="operator")
    s.ensure_schema()
    s.upsert_strategy()
    return s


def _alpaca_dashboard_url() -> str:
    base = (os.getenv("ALPACA_BASE_URL") or "").lower()
    if "paper" in base:
        return "https://app.alpaca.markets/paper/dashboard/overview"
    return "https://app.alpaca.markets/live/dashboard/overview"


def _broker_qty_from_snapshot(snapshot) -> float | None:
    if isinstance(snapshot, str):
        try:
            import json
            snapshot = json.loads(snapshot)
        except (ValueError, TypeError):
            return None
    if not isinstance(snapshot, dict):
        return None
    val = snapshot.get("broker_qty")
    try:
        return float(val) if val is not None else None
    except (TypeError, ValueError):
        return None


def _render_unmanaged_section(broker_only_df) -> None:
    st.subheader("Unmanaged broker positions")
    st.caption(
        "Positions held at Alpaca with no managed MySQL row. Closing here "
        "submits a market order tagged `role=exit` (operator/cleanslate) and "
        "resolves the strike. Audit written to `runtime/operator_close_audit_*.jsonl`."
    )
    alpaca_link = _alpaca_dashboard_url()

    for _, row in broker_only_df.iterrows():
        strike_id = int(row["id"])
        symbol = str(row["symbol"])
        snapshot = row.get("last_observed_state")
        qty = _broker_qty_from_snapshot(snapshot)
        last_seen = row.get("last_seen_at")

        confirm_key = f"recon_confirm_close_{strike_id}"
        note_key = f"recon_close_note_{strike_id}"

        with st.container(border=True):
            head_cols = st.columns([2, 2, 3, 2])
            head_cols[0].markdown(f"**{symbol}**")
            head_cols[1].markdown(
                f"qty: `{qty:+.4f}`" if qty is not None else "qty: —"
            )
            head_cols[2].markdown(
                f"last seen: `{last_seen}`" if last_seen else "last seen: —"
            )
            head_cols[3].markdown(f"[view in Alpaca]({alpaca_link})")

            note_col, btn_col = st.columns([4, 2])
            note = note_col.text_input(
                "Operator note (≥3 chars, required)",
                key=note_key,
                placeholder="e.g. manual leftover from yesterday",
            )

            confirming = st.session_state.get(confirm_key, False)

            if not confirming:
                disabled = len((note or "").strip()) < 3
                if btn_col.button(
                    "Close on Alpaca",
                    key=f"recon_close_btn_{strike_id}",
                    disabled=disabled,
                    type="primary",
                ):
                    st.session_state[confirm_key] = True
                    st.rerun()
            else:
                st.warning(
                    f"Confirm: submit a market order to close **{symbol}** "
                    f"(broker qty `{qty:+.4f}`)?"
                    if qty is not None
                    else f"Confirm: submit a market order to close **{symbol}**?"
                )
                c_col, x_col = st.columns([1, 1])
                if c_col.button(
                    "Confirm close",
                    key=f"recon_confirm_btn_{strike_id}",
                    type="primary",
                ):
                    try:
                        result = close_broker_only_strike(
                            store=_get_store(),
                            alpaca=_get_alpaca(),
                            strike_id=strike_id,
                            operator_note=note,
                        )
                    except Exception as exc:
                        st.error(f"close failed: {exc}")
                        st.session_state[confirm_key] = False
                    else:
                        if result.status == "submitted":
                            st.success(
                                f"submitted close: {symbol} "
                                f"order_id={result.alpaca_order_id} "
                                f"coid={result.coid}"
                            )
                        elif result.status == "already_flat":
                            st.info(
                                f"{symbol} was already flat — strike resolved."
                            )
                        elif result.status == "submit_failed":
                            st.error(
                                f"submit failed: {result.error}. "
                                "strike left unresolved for retry."
                            )
                        else:
                            st.info(f"status: {result.status}")
                        st.session_state[confirm_key] = False
                        st.rerun()
                if x_col.button("Cancel", key=f"recon_cancel_btn_{strike_id}"):
                    st.session_state[confirm_key] = False
                    st.rerun()


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

    # ── Strikes ───────────────────────────────────────────────────────
    strikes = repo.get_unresolved_strikes()

    # New: actionable surface for broker_only strikes only.
    broker_only = (
        strikes[strikes["direction"] == "broker_only"]
        if not strikes.empty else strikes
    )
    if not broker_only.empty:
        _render_unmanaged_section(broker_only)

    st.subheader("Unresolved strikes")
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
        with st.expander("How to resolve a strike via CLI"):
            st.markdown(
                "All resolutions other than the broker_only close button "
                "above go through the operator CLI for audit-trail reasons. "
                "Connect to the trader container:"
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
