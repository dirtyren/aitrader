"""Approval module: read/write runtime/wfo/active/live_overrides.yaml + audit.

Atomicity: tmp + os.replace. Last writer wins under concurrent approves.
"""
from __future__ import annotations
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import yaml


class GateFailedApproveError(ValueError):
    """Raised when attempting to approve a candidate that did not pass the gate."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def read_active(active_path: Path) -> dict:
    if not Path(active_path).exists():
        return {"symbols": {}}
    payload = yaml.safe_load(Path(active_path).read_text()) or {}
    payload.setdefault("symbols", {})
    return payload


def _append_audit(audit_path: Path, record: dict) -> None:
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    with audit_path.open("a") as f:
        f.write(json.dumps(record) + "\n")


def approve_symbol(active_path: Path, audit_path: Path, symbol: str,
                   candidate: dict, run_id: str,
                   operator: str = "dashboard") -> None:
    """Replace symbols.<symbol> in active file with candidate (+ provenance)."""
    if not candidate.get("metadata", {}).get("passed", True):
        raise GateFailedApproveError(
            f"Candidate for {symbol} did not pass the gate"
        )
    payload = read_active(active_path)
    prev = payload["symbols"].get(symbol, {})
    prev_run_id = (prev.get("_provenance") or {}).get("run_id")
    new_entry = {
        k: v for k, v in candidate.items()
        if k in ("timeframe", "setup", "setup_params",
                 "position_management", "metadata")
    }
    new_entry["_provenance"] = {
        "run_id": run_id,
        "approved_at": _now_iso(),
        "approved_by": operator,
    }
    payload["symbols"][symbol] = new_entry
    _write_atomic(Path(active_path),
                  yaml.safe_dump(payload, sort_keys=False))
    _append_audit(Path(audit_path), {
        "ts": _now_iso(), "action": "approve", "symbol": symbol,
        "run_id": run_id, "prev_run_id": prev_run_id, "operator": operator,
    })


def revert_symbol(active_path: Path, audit_path: Path, symbol: str,
                  operator: str = "dashboard") -> None:
    payload = read_active(active_path)
    prev = payload["symbols"].pop(symbol, None)
    prev_run_id = (prev.get("_provenance") or {}).get("run_id") if prev else None
    _write_atomic(Path(active_path),
                  yaml.safe_dump(payload, sort_keys=False))
    _append_audit(Path(audit_path), {
        "ts": _now_iso(), "action": "revert", "symbol": symbol,
        "run_id": None, "prev_run_id": prev_run_id, "operator": operator,
    })


def read_audit_tail(audit_path: Path, n: int = 50) -> list[dict]:
    if not Path(audit_path).exists():
        return []
    lines = Path(audit_path).read_text().strip().splitlines()
    return [json.loads(line) for line in lines[-n:]]
