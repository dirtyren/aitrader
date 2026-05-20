"""Parameter-grid expansion for WFO combos."""
from __future__ import annotations
import hashlib
import itertools
import json
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ParamCombo:
    setup: str
    setup_values: dict[str, Any]
    pm_values: dict[str, Any]
    fingerprint: str


def _fingerprint(setup: str, setup_values: dict, pm_values: dict) -> str:
    payload = json.dumps(
        {"setup": setup, "setup_values": setup_values, "pm_values": pm_values},
        sort_keys=True, separators=(",", ":"), default=str,
    )
    return hashlib.blake2b(payload.encode("utf-8"), digest_size=10).hexdigest()


def _cartesian(spec: dict[str, list[Any]]) -> list[dict[str, Any]]:
    if not spec:
        return [{}]
    keys = sorted(spec.keys())
    value_lists = [spec[k] for k in keys]
    return [dict(zip(keys, combo)) for combo in itertools.product(*value_lists)]


def expand_grid(grid_spec: dict[str, dict[str, list[Any]]],
                pm_spec: dict[str, list[Any]]) -> list[ParamCombo]:
    """Cross-multiply per-setup ranges with position-management ranges.

    A setup with `enabled: [False]` produces zero combos (disabled).
    """
    pm_combos = _cartesian(pm_spec)
    out: list[ParamCombo] = []
    for setup in sorted(grid_spec.keys()):
        spec = grid_spec[setup]
        if spec.get("enabled") == [False]:
            continue
        for setup_values in _cartesian(spec):
            for pm_values in pm_combos:
                fp = _fingerprint(setup, setup_values, pm_values)
                out.append(ParamCombo(setup=setup, setup_values=setup_values,
                                      pm_values=pm_values, fingerprint=fp))
    return out
