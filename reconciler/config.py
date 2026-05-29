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


@dataclass(frozen=True)
class ReconcilerConfig:
    interval_s: int
    strike_threshold: int
    strike_min_gap_s: int
    qty_eps: float
    shadow_mode: bool
    state_file_path: str
    heartbeat_stale_after_s: int

    @classmethod
    def from_env(cls) -> "ReconcilerConfig":
        return cls(
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
        )
