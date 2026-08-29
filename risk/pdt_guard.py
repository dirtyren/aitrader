"""Pattern Day Trader precondition check.

FINRA limits a US margin account under $25,000 equity to 3 day trades per
rolling 5 business days; a breach restricts the account to closing-only for
90 days. Opening Drive opens and closes every position intraday, generating
4+ day trades per session, so it would trip the rule on its first day.

Paper accounts do not enforce PDT. That is precisely why this guard is
tested rather than assumed: the failure surfaces only in live trading, and
the cost there is a 90-day restriction, not an error message.

This is a BOOT PRECONDITION, not an entry filter. Refusing to start is far
better than discovering the problem mid-session with positions open.
"""
from __future__ import annotations

import logging
import math

logger = logging.getLogger(__name__)


class PDTViolation(Exception):
    """Raised when the account cannot safely day-trade this strategy."""


def check_pdt_headroom(
    account: dict,
    *,
    min_equity: float = 25_000.0,
    enabled: bool = True,
) -> None:
    """Raise PDTViolation when the account lacks day-trading headroom.

    ``enabled`` is passed from config rather than sniffed from the account
    dict: Alpaca does not expose a reliable paper/live discriminator in the
    account payload, so the caller (which knows its base URL) decides.
    """
    if not enabled:
        logger.info("PDT_GUARD_DISABLED — skipping headroom check")
        return

    if account.get("trading_blocked") or account.get("account_blocked"):
        raise PDTViolation(
            "account is blocked for trading — refusing to start"
        )

    raw = account.get("equity")
    if raw is None:
        raise PDTViolation(
            "account payload has no 'equity' field — refusing to start "
            "rather than assume PDT headroom exists"
        )
    try:
        equity = float(raw)
    except (TypeError, ValueError):
        raise PDTViolation(
            f"could not parse account 'equity' value {raw!r} — refusing to "
            f"start rather than assume PDT headroom exists"
        ) from None

    if not math.isfinite(equity):
        raise PDTViolation(
            f"account 'equity' parsed to non-finite value {equity!r} — refusing to start"
        )

    if equity < min_equity:
        raise PDTViolation(
            f"account equity {equity:.2f} is below the PDT threshold "
            f"{min_equity:.0f}. This strategy generates 4+ day trades per "
            f"session, which would trigger a 90-day closing-only "
            f"restriction. Set risk.pdt_guard_enabled: false only for a "
            f"paper account."
        )

    logger.info(
        "PDT_GUARD_OK equity=%.2f threshold=%.0f flagged_pdt=%s",
        equity, min_equity, bool(account.get("pattern_day_trader")),
    )
