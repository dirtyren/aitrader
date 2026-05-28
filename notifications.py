"""Telegram notification helpers for aitrader.

Sends alerts via Telegram Bot API when positions are opened.
Reads TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID from the environment.
Both env vars must be set; if either is missing, notifications are silently
skipped with a debug-level log message.
"""

from __future__ import annotations

import logging
import os

import requests

log = logging.getLogger(__name__)

_TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"


def _load_telegram_config() -> tuple[str | None, str | None]:
    """Load Telegram credentials from environment.

    Returns (bot_token, chat_id) or (None, None) if not configured.
    """
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return None, None
    return token, chat_id


def send_position_open_alert(
    strategy_name: str,
    symbol: str,
    side: str,
    qty: float,
    entry_px: float,
    stop_px: float | None = None,
    target_px: float | None = None,
    setup_name: str = "",
    asset_class: str = "",
    adopted: bool = False,
) -> bool:
    """Send a Telegram alert when a position is opened.

    Returns True if the message was sent successfully, False if skipped or failed.
    """
    token, chat_id = _load_telegram_config()
    if token is None:
        log.debug("TELEGRAM_NOTIFY_SKIPPED symbol=%s — TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set",
                   symbol)
        return False

    # ── Build a trader-readable message ──────────────────────────────────
    side_emoji = "🟢" if side == "long" else "🔴"
    side_label = side.upper()

    # Format quantities: crypto uses 8 decimals, equity uses whole shares
    if asset_class == "crypto":
        qty_str = f"{qty:.6f}"
    else:
        qty_str = f"{int(qty)}" if qty == int(qty) else f"{qty:.2f}"

    entry_str = f"${entry_px:.2f}" if entry_px >= 1 else f"${entry_px:.6f}"

    lines = [f"{side_emoji} {strategy_name} opened {side_label} {symbol}"]

    details = f"Qty: {qty_str} @ {entry_str}"
    if setup_name:
        details += f" | Setup: {setup_name}"
    lines.append(details)

    stop_target = []
    if stop_px is not None:
        stop_str = f"${stop_px:.2f}" if stop_px >= 1 else f"${stop_px:.6f}"
        stop_target.append(f"Stop: {stop_str}")
    if target_px is not None:
        tgt_str = f"${target_px:.2f}" if target_px >= 1 else f"${target_px:.6f}"
        stop_target.append(f"Target: {tgt_str}")
    if stop_target:
        lines.append(" | ".join(stop_target))

    # Estimate R if both stop and entry are known
    if stop_px is not None and entry_px:
        risk_per_share = abs(entry_px - stop_px)
        if risk_per_share > 0 and target_px is not None:
            r_target = abs(target_px - entry_px) / risk_per_share
            lines.append(f"R: {r_target:.2f}R")
        lines.append(f"Risk: ${risk_per_share * qty:.2f}")

    if adopted:
        lines.append("⚠️ Recovered from broker (post-restart)")

    message = "\n".join(lines)

    # ── Send via Telegram Bot API ────────────────────────────────────────
    try:
        resp = requests.post(
            _TELEGRAM_API.format(token=token),
            json={
                "chat_id": chat_id,
                "text": message,
                "parse_mode": "HTML",  # won't use HTML but safe to pass
            },
            timeout=10,
        )
        if resp.status_code == 200:
            log.info("TELEGRAM_NOTIFY_SENT symbol=%s strategy=%s", symbol, strategy_name)
            return True
        else:
            log.warning("TELEGRAM_NOTIFY_FAILED symbol=%s status=%s body=%s",
                        symbol, resp.status_code, resp.text[:200])
            return False
    except requests.RequestException as exc:
        log.warning("TELEGRAM_NOTIFY_ERROR symbol=%s: %s", symbol, exc)
        return False


def send_reconcile_alert(
    direction: str,
    symbol: str,
    strategy_name: str | None,
    snapshot: dict,
    strike_count: int,
    strike_threshold: int,
) -> bool:
    """Send a Telegram alert for a confirmed reconciliation anomaly.

    Returns True if sent, False if Telegram is not configured.
    """
    token, chat_id = _load_telegram_config()
    if token is None:
        log.debug("RECONCILE_TELEGRAM_SKIPPED — TELEGRAM_BOT_TOKEN/CHAT_ID not set")
        return False

    severity = "🚨 FROZEN" if strike_count >= strike_threshold else "⚠️ STRIKE"
    parts: list[str] = [
        f"{severity} reconciliation: {direction} on {symbol}",
        f"strike {strike_count}/{strike_threshold}",
    ]
    if strategy_name:
        parts.insert(1, f"strategy={strategy_name}")
    if "mysql_sum" in snapshot:
        parts.append(f"mysql_sum={snapshot.get('mysql_sum')}")
    if "broker_qty" in snapshot:
        parts.append(f"broker_qty={snapshot.get('broker_qty')}")
    if "mysql_qty" in snapshot:
        parts.append(f"mysql_qty={snapshot.get('mysql_qty')}")

    text = "\n".join(parts)
    try:
        resp = requests.post(
            _TELEGRAM_API.format(token=token),
            json={"chat_id": chat_id, "text": text},
            timeout=5,
        )
        return resp.ok
    except Exception as exc:
        log.warning("RECONCILE_TELEGRAM_FAILED err=%s", exc)
        return False
