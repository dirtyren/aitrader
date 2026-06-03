"""Configuration for the reconciler service.

All knobs are read from environment variables with conservative defaults
(see spec section 3 "Defaults & tunables").
"""
from __future__ import annotations

import os
from dataclasses import dataclass


_TRUTHY = frozenset({"true", "1", "yes"})


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in _TRUTHY


_VALID_ASSET_CLASSES = frozenset({"equity", "crypto"})


@dataclass(frozen=True)
class ReconcilerConfig:
    interval_s: int
    strike_threshold: int
    strike_min_gap_s: int
    qty_eps: float
    shadow_mode: bool
    state_file_path: str
    heartbeat_stale_after_s: int
    # Auto-close chunking: Alpaca rejects single orders whose notional exceeds
    # $200k. Cap below the limit so post-rounding/price-drift each child order
    # stays clear; oversized auto-closes split into N chunks.
    auto_close_max_notional_usd: float
    # Auto-close dust threshold: positions with notional below this value are
    # too small to submit (Alpaca enforces a minimum qty per asset, and many
    # close-rejection loops bottom out at floating-point dust). Treat them as
    # effectively flat: resolve the strike with reason='auto_close_dust' and
    # emit an event for audit.
    auto_close_dust_usd: float
    # The asset class this reconciler instance owns. Each side runs in its
    # own container so an Alpaca outage on one account can't short-circuit
    # the other. Required by from_env (RECONCILER_ASSET_CLASS); optional in
    # tests so the strike/event scoping path can be exercised without the
    # full container env. Strikes/events are stamped with this value.
    asset_class: str | None = None
    # Manual-close detection (specs/manual-close-cooldown.md).
    # confirm_cycles: how many consecutive cycles a candidate must persist
    # before the cooldown row is created (defaults to 2 — gives engine-issued
    # exits time to propagate without misclassifying them).
    # cooldown_min: cooldown window in minutes (0 = audit-only — events emit
    # but the filter never blocks; the cooldown_until equals started_at).
    manual_close_confirm_cycles: int = 2
    manual_close_cooldown_min: int = 60

    @classmethod
    def from_env(cls) -> "ReconcilerConfig":
        asset_class = os.environ.get("RECONCILER_ASSET_CLASS", "").strip().lower()
        if asset_class not in _VALID_ASSET_CLASSES:
            raise RuntimeError(
                f"RECONCILER_ASSET_CLASS must be one of "
                f"{sorted(_VALID_ASSET_CLASSES)}, got {asset_class!r}"
            )
        return cls(
            asset_class=asset_class,
            interval_s=int(os.environ.get("RECONCILE_INTERVAL_S", "30")),
            strike_threshold=int(os.environ.get("RECONCILE_STRIKE_THRESHOLD", "3")),
            strike_min_gap_s=int(os.environ.get("RECONCILE_STRIKE_MIN_GAP_S", "60")),
            qty_eps=float(os.environ.get("RECONCILE_QTY_EPS", "1e-6")),
            shadow_mode=_env_bool("SHADOW_MODE", default=False),
            state_file_path=os.environ.get(
                "RECONCILE_STATE_FILE", "/app/runtime/reconciler_state.json"
            ),
            heartbeat_stale_after_s=int(os.environ.get(
                "RECONCILE_HEARTBEAT_STALE_AFTER_S", "300"
            )),
            auto_close_max_notional_usd=float(os.environ.get(
                "RECONCILE_AUTO_CLOSE_MAX_NOTIONAL_USD", "190000"
            )),
            auto_close_dust_usd=float(os.environ.get(
                "RECONCILE_AUTO_CLOSE_DUST_USD", "1.0"
            )),
            manual_close_confirm_cycles=int(os.environ.get(
                "MANUAL_CLOSE_CONFIRM_CYCLES", "2"
            )),
            manual_close_cooldown_min=int(os.environ.get(
                "MANUAL_CLOSE_COOLDOWN_MIN", "60"
            )),
        )
