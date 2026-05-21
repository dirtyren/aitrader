"""Job state machine — file-based, atomic-rename transitions.

Layout:
  <jobs_root>/queue/<job_id>.json     queued, oldest-first
  <jobs_root>/active/<job_id>.json    0 or 1 file, the running job
  <jobs_root>/history/<job_id>.json   completed/cancelled/failed
  <jobs_root>/configs/<job_id>.yaml   frozen wfo.yaml passed to the CLI
  <jobs_root>/active/<job_id>.cancel  sentinel for graceful cancel

Status transitions:
  queued -> starting -> running -> (completed | cancelled | failed)
"""
from __future__ import annotations
import json
import logging
import os
from dataclasses import dataclass, asdict, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

import yaml

logger = logging.getLogger("wfo.job_state")

JobStatus = Literal["queued", "starting", "running",
                    "completed", "cancelled", "failed"]


@dataclass(frozen=True)
class ProgressInfo:
    total_pairs: int = 0
    completed_pairs: int = 0
    current_symbol: str | None = None
    current_timeframe: str | None = None
    rows_written: int = 0
    elapsed_s: float = 0.0
    eta_s: float | None = None


@dataclass(frozen=True)
class JobRecord:
    job_id: str
    run_id: str
    status: JobStatus
    queued_at: str
    form_payload: dict
    wfo_config_path: str
    started_at: str | None = None
    completed_at: str | None = None
    pid: int | None = None
    exit_code: int | None = None
    error: str | None = None
    progress: ProgressInfo = field(default_factory=ProgressInfo)

    def to_dict(self) -> dict:
        d = asdict(self)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "JobRecord":
        prog = d.get("progress") or {}
        return cls(
            job_id=d["job_id"], run_id=d["run_id"], status=d["status"],
            queued_at=d["queued_at"], form_payload=d.get("form_payload") or {},
            wfo_config_path=d["wfo_config_path"],
            started_at=d.get("started_at"), completed_at=d.get("completed_at"),
            pid=d.get("pid"), exit_code=d.get("exit_code"),
            error=d.get("error"),
            progress=ProgressInfo(**prog) if prog else ProgressInfo(),
        )


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _ensure_dirs(jobs_root: Path) -> None:
    for sub in ("queue", "active", "history", "configs"):
        (jobs_root / sub).mkdir(parents=True, exist_ok=True)


def _write_atomic(path: Path, payload: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, default=str))
    os.replace(tmp, path)


def _path_for(jobs_root: Path, status_dir: str, job_id: str) -> Path:
    return jobs_root / status_dir / f"{job_id}.json"


def _read_record(path: Path) -> JobRecord:
    return JobRecord.from_dict(json.loads(path.read_text()))


def _build_run_id(job_id: str) -> str:
    """Reuse job_id verbatim as run_id. The CLI's deterministic-hash run_id is
    overridden with this one via --run-id so dashboard and engine agree."""
    return job_id


def enqueue(jobs_root: Path, *, payload: dict, wfo_template: dict,
            job_id: str | None = None) -> JobRecord:
    """Write the frozen wfo_template to disk and queue a JobRecord.

    `wfo_template` is the final config the CLI will read. Callers from
    `forms.render_form` pre-merge their FormPayload via
    `forms.merge_payload_into_template` before calling here. `payload` is
    stored in the JobRecord for display/audit and is not re-merged."""
    _ensure_dirs(jobs_root)
    if job_id is None:
        job_id = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S_%f")[:-3]
    config_path = jobs_root / "configs" / f"{job_id}.yaml"
    config_path.write_text(yaml.safe_dump(wfo_template, sort_keys=False))
    rec = JobRecord(
        job_id=job_id, run_id=_build_run_id(job_id), status="queued",
        queued_at=_now_iso(), form_payload=payload,
        wfo_config_path=str(config_path),
    )
    _write_atomic(_path_for(jobs_root, "queue", job_id), rec.to_dict())
    logger.info("WFO_JOB_ENQUEUED job_id=%s run_id=%s", rec.job_id, rec.run_id)
    return rec


def claim_next(jobs_root: Path) -> JobRecord | None:
    _ensure_dirs(jobs_root)
    queue_dir = jobs_root / "queue"
    files = sorted(queue_dir.glob("*.json"))
    if not files:
        return None
    src = files[0]
    rec = _read_record(src)
    rec = replace(rec, status="starting")
    dst = _path_for(jobs_root, "active", rec.job_id)
    _write_atomic(dst, rec.to_dict())
    src.unlink()
    logger.info("WFO_JOB_CLAIMED job_id=%s run_id=%s", rec.job_id, rec.run_id)
    return rec


def mark_running(jobs_root: Path, job_id: str, *, pid: int) -> JobRecord:
    path = _path_for(jobs_root, "active", job_id)
    rec = _read_record(path)
    rec = replace(rec, status="running", pid=pid, started_at=_now_iso())
    _write_atomic(path, rec.to_dict())
    logger.info("WFO_JOB_RUNNING job_id=%s pid=%d", rec.job_id, pid)
    return rec


def update_progress(jobs_root: Path, job_id: str,
                    progress: ProgressInfo) -> JobRecord:
    path = _path_for(jobs_root, "active", job_id)
    rec = _read_record(path)
    rec = replace(rec, progress=progress)
    _write_atomic(path, rec.to_dict())
    return rec


def finalize(jobs_root: Path, job_id: str, *, status: JobStatus,
             exit_code: int | None, error: str | None) -> JobRecord:
    if status not in ("completed", "cancelled", "failed"):
        raise ValueError(f"finalize requires terminal status, got {status!r}")
    src = _path_for(jobs_root, "active", job_id)
    rec = _read_record(src)
    rec = replace(rec, status=status, exit_code=exit_code, error=error,
                  completed_at=_now_iso())
    dst = _path_for(jobs_root, "history", job_id)
    _write_atomic(dst, rec.to_dict())
    src.unlink()
    cancel = jobs_root / "active" / f"{job_id}.cancel"
    if cancel.exists():
        cancel.unlink()
    logger.info("WFO_JOB_FINALIZED job_id=%s status=%s exit_code=%s error=%s",
                rec.job_id, status, exit_code, error)
    return rec


def list_jobs(jobs_root: Path) -> tuple[list[JobRecord], list[JobRecord], list[JobRecord]]:
    _ensure_dirs(jobs_root)

    def _load(d: Path) -> list[JobRecord]:
        return [_read_record(p) for p in sorted(d.glob("*.json"))]

    return (_load(jobs_root / "queue"),
            _load(jobs_root / "active"),
            _load(jobs_root / "history"))


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def detect_orphans(jobs_root: Path) -> list[JobRecord]:
    _ensure_dirs(jobs_root)
    out: list[JobRecord] = []
    for p in (jobs_root / "active").glob("*.json"):
        rec = _read_record(p)
        if rec.pid is None:
            continue
        if not _pid_alive(rec.pid):
            out.append(rec)
    return out


def write_cancel_sentinel(jobs_root: Path, job_id: str) -> None:
    _ensure_dirs(jobs_root)
    (jobs_root / "active" / f"{job_id}.cancel").touch()
    logger.info("WFO_JOB_CANCEL_REQUESTED job_id=%s", job_id)


def has_cancel_sentinel(jobs_root: Path, job_id: str) -> bool:
    return (jobs_root / "active" / f"{job_id}.cancel").exists()
