"""Dashboard Settings tab — manage per-asset-class Alpaca credentials.

Credentials live in MySQL (broker_credentials table). This tab is the
write-path: an operator types a new key/secret, runs a required
test-connection step (GET /v2/account), and only then can save. Saved
changes require a trader-container restart to take effect; the success
banner lists which containers to restart.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import streamlit as st

from broker import credentials as creds_mod
from broker.credentials import AlpacaCreds
from ui.data.strategy_configs import list_by_asset_class


# ---------------------------------------------------------------------------
# Pure-logic helpers (testable without Streamlit)
# ---------------------------------------------------------------------------

def _test_connection(api_key: str, secret_key: str, base_url: str) -> tuple[bool, str]:
    creds = AlpacaCreds(
        asset_class="equity",  # placeholder; resolver doesn't validate here
        api_key=api_key, secret_key=secret_key, base_url=base_url,
        source="db",
    )
    return creds_mod.test_connection(creds)


def _upsert(asset_class: str, api_key: str, secret_key: str, base_url: str) -> None:
    creds_mod.upsert(asset_class, api_key, secret_key, base_url)


def _set_account_number(asset_class: str, account_number: str) -> None:
    from state.mysql_store import MySQLStore
    s = MySQLStore(strategy_name="settings_tab")
    s.ensure_schema()
    s.set_broker_credentials_account_number(asset_class, account_number)


def save_credentials(
    asset_class: str,
    api_key: str,
    secret_key: str,
    base_url: str,
) -> tuple[bool, str]:
    """Test connection then persist on success.

    Returns (True, account_number_message) when the credentials are saved.
    Returns (False, error_reason) when the test fails — no DB write.
    """
    ok, msg = _test_connection(api_key, secret_key, base_url)
    if not ok:
        return False, msg
    _upsert(asset_class, api_key, secret_key, base_url)
    # Cache the account number from the successful test for the dashboard header.
    account_number = msg.split(" ", 1)[0]  # strip trailing "(warning: …)" if present
    if account_number:
        try:
            _set_account_number(asset_class, account_number)
        except Exception:
            pass  # best-effort; primary save already succeeded
    return True, msg


def containers_for_asset_class(
    asset_class: str,
    config_dir: Path = Path("config"),
) -> list[str]:
    """Trader container names that need a restart after a credential save.

    Naming convention from docker-compose.yml: trader-<strategy-name>.
    """
    names = list_by_asset_class(asset_class, config_dir=config_dir)
    return [f"trader-{name}" for name in names]


def _mask_key(key: str | None) -> str:
    if not key:
        return "—"
    if len(key) <= 8:
        return key[0] + "***"
    return f"{key[:4]}***{key[-2:]}"


# ---------------------------------------------------------------------------
# Streamlit render
# ---------------------------------------------------------------------------

def render() -> None:
    st.subheader("Alpaca credentials")
    st.caption(
        "Per-asset-class API keys. Saving requires a successful test "
        "connection; trader containers must be restarted to apply."
    )
    eq, cr = st.columns(2)
    with eq:
        _render_card("equity")
    with cr:
        _render_card("crypto")


def _render_card(asset_class: str) -> None:
    with st.container(border=True):
        st.markdown(f"### {asset_class.capitalize()}")
        try:
            from state.mysql_store import MySQLStore
            store = MySQLStore(strategy_name="settings_tab_view")
            store.ensure_schema()
            row = store.get_broker_credentials(asset_class)
        except Exception as exc:
            st.error(f"DB error, cannot edit: {exc}")
            return

        if row is None:
            st.markdown("**Status:** _not configured_")
            current_base = "https://paper-api.alpaca.markets"
            current_account = ""
            updated_at: datetime | None = None
        else:
            st.markdown(
                f"**Status:** account `{row.get('account_number') or '—'}`  "
                f"key `{_mask_key(row['api_key'])}`"
            )
            current_base = row["base_url"]
            current_account = row.get("account_number") or ""
            updated_at = row.get("updated_at")

        if updated_at is not None:
            st.caption(f"Last updated: `{updated_at.isoformat(sep=' ', timespec='seconds')}`")

        edit_key = f"settings_edit_{asset_class}"
        editing = st.session_state.get(edit_key, False)

        if not editing:
            if st.button(f"Edit {asset_class}", key=f"settings_edit_btn_{asset_class}"):
                st.session_state[edit_key] = True
                st.rerun()
            return

        st.markdown("---")
        api_key = st.text_input(
            "API key", key=f"settings_api_{asset_class}", type="password",
        )
        secret_key = st.text_input(
            "Secret key", key=f"settings_secret_{asset_class}", type="password",
        )
        base_url = st.text_input(
            "Base URL", value=current_base, key=f"settings_base_{asset_class}",
        )

        test_state_key = f"settings_test_ok_{asset_class}"
        test_msg_key = f"settings_test_msg_{asset_class}"

        cc, ss, xx = st.columns([1, 1, 1])
        if cc.button("Test connection", key=f"settings_test_btn_{asset_class}"):
            ok, msg = _test_connection(api_key, secret_key, base_url)
            st.session_state[test_state_key] = ok
            st.session_state[test_msg_key] = msg

        save_disabled = not st.session_state.get(test_state_key, False) or not (
            api_key and secret_key and base_url
        )
        if ss.button(
            "Save", key=f"settings_save_btn_{asset_class}",
            type="primary", disabled=save_disabled,
        ):
            ok, msg = save_credentials(asset_class, api_key, secret_key, base_url)
            if ok:
                containers = containers_for_asset_class(asset_class)
                st.success(
                    f"Saved {asset_class} credentials (account {msg}).  "
                    f"**Restart these containers to apply:**  "
                    + ", ".join(f"`{c}`" for c in containers)
                )
                # Clear cached AlpacaClient so the dashboard picks up new creds.
                from ui.tabs.strategies_tab import _get_alpaca
                _get_alpaca.clear()
                st.session_state[edit_key] = False
                st.session_state[test_state_key] = False
                st.session_state[test_msg_key] = ""
                st.rerun()
            else:
                st.error(f"Save failed: {msg}")
        if xx.button("Cancel", key=f"settings_cancel_btn_{asset_class}"):
            st.session_state[edit_key] = False
            st.session_state[test_state_key] = False
            st.session_state[test_msg_key] = ""
            st.rerun()

        # Inline test result.
        msg = st.session_state.get(test_msg_key, "")
        if msg:
            if st.session_state.get(test_state_key):
                st.success(f"✓ Connection OK — {msg}")
            else:
                st.error(f"✗ {msg}")
