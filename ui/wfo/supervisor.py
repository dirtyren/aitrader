"""WFO job supervisor: serializes queued jobs through subprocess execution.

The default subprocess_factory spawns `python -m scripts.run_wfo` with the
job's frozen config. Tests inject a stub. Supervisor is intentionally
single-threaded per drain_once(); the dashboard runs drain_once() in a
background thread guarded by an flock on supervisor.lock.
"""
from __future__ import annotations
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable

from ui.wfo.job_state import (
    JobRecord, claim_next, mark_running, update_progress, finalize,
    has_cancel_sentinel, detect_orphans, ProgressInfo,
)

logger = logging.getLogger("wfo.supervisor")

ProcLike = subprocess.Popen  # protocol — only poll/wait/terminate/kill/pid used


def _default_factory(record: JobRecord) -> ProcLike:
    cmd = [sys.executable, "-m", "scripts.run_wfo",
           "--config", record.wfo_config_path, "--run-id", record.run_id]
    return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)


class Supervisor:
    def __init__(self, jobs_root: Path, *,
                 subprocess_factory: Callable[[JobRecord], ProcLike] = _default_factory,
                 poll_interval_s: float = 2.0,
                 cancel_grace_s: float = 10.0):
        self.jobs_root = Path(jobs_root)
        self._factory = subprocess_factory
        self._poll = poll_interval_s
        self._grace = cancel_grace_s

    def reap_orphans(self) -> int:
        n = 0
        for rec in detect_orphans(self.jobs_root):
            finalize(self.jobs_root, rec.job_id, status="failed",
                     exit_code=None, error="orphaned (dashboard restart)")
            n += 1
        return n

    def drain_once(self) -> bool:
        """Claim and run the oldest queued job. Returns True iff one ran."""
        rec = claim_next(self.jobs_root)
        if rec is None:
            return False
        try:
            proc = self._factory(rec)
            mark_running(self.jobs_root, rec.job_id, pid=proc.pid)
            self._track(rec, proc)
        except Exception as exc:
            logger.exception("supervisor drain failed for %s", rec.job_id)
            finalize(self.jobs_root, rec.job_id, status="failed",
                     exit_code=None, error=repr(exc))
        return True

    def _track(self, rec: JobRecord, proc: ProcLike) -> None:
        started_at = time.time()
        cancel_sent_at: float | None = None
        while True:
            rc = proc.poll()
            if rc is not None:
                break
            if has_cancel_sentinel(self.jobs_root, rec.job_id):
                if cancel_sent_at is None:
                    proc.terminate()
                    cancel_sent_at = time.time()
                elif time.time() - cancel_sent_at >= self._grace:
                    proc.kill()
                    proc.wait(timeout=self._grace)
                    rc = proc.returncode
                    break
            self._tick_progress(rec, started_at)
            time.sleep(self._poll)

        if rc == 0:
            finalize(self.jobs_root, rec.job_id, status="completed",
                     exit_code=0, error=None)
        elif cancel_sent_at is not None:
            finalize(self.jobs_root, rec.job_id, status="cancelled",
                     exit_code=rc, error=None)
        else:
            finalize(self.jobs_root, rec.job_id, status="failed",
                     exit_code=rc, error=f"subprocess exit {rc}")

    def _tick_progress(self, rec: JobRecord, started_at: float) -> None:
        """Read manifest.json + parquet row count from runtime/wfo/<run_id>/.
        Best-effort; failures swallowed (the run dir may not exist yet)."""
        try:
            run_dir = Path("runtime/wfo") / rec.run_id
            manifest_path = run_dir / "manifest.json"
            parquet_path = run_dir / "results.parquet"
            current_symbol = current_tf = None
            total_pairs = completed_pairs = 0
            rows_written = 0
            if manifest_path.exists():
                import json
                m = json.loads(manifest_path.read_text())
                total_pairs = int(m.get("total_pairs") or 0)
                completed_pairs = int(m.get("completed_pairs") or 0)
                current_symbol = m.get("current_symbol")
                current_tf = m.get("current_timeframe")
            if parquet_path.exists():
                try:
                    import pyarrow.parquet as pq
                    rows_written = pq.ParquetFile(parquet_path).metadata.num_rows
                except Exception:
                    rows_written = 0
            elapsed = time.time() - started_at
            eta = None
            if completed_pairs and total_pairs:
                rate = completed_pairs / elapsed
                if rate > 0:
                    eta = (total_pairs - completed_pairs) / rate
            update_progress(self.jobs_root, rec.job_id, ProgressInfo(
                total_pairs=total_pairs, completed_pairs=completed_pairs,
                current_symbol=current_symbol, current_timeframe=current_tf,
                rows_written=rows_written, elapsed_s=elapsed, eta_s=eta,
            ))
        except Exception:
            logger.debug("progress tick failed", exc_info=True)


# --- background thread plumbing --------------------------------------------

_singleton_lock = threading.Lock()
_singleton: Supervisor | None = None
_singleton_thread: threading.Thread | None = None


def get_or_start_supervisor(jobs_root: Path) -> Supervisor:
    """Idempotent: returns the running supervisor or starts a new one.

    Guarded by a process-local threading.Lock; cross-process exclusion uses
    `<jobs_root>/supervisor.lock` (flock) acquired inside the thread."""
    global _singleton, _singleton_thread
    with _singleton_lock:
        if _singleton is not None:
            return _singleton
        _singleton = Supervisor(jobs_root)
        _singleton_thread = threading.Thread(
            target=_run_loop, args=(_singleton,), daemon=True,
            name="wfo-supervisor")
        _singleton_thread.start()
        return _singleton


def _run_loop(sup: Supervisor) -> None:
    import fcntl
    lock_path = sup.jobs_root / "supervisor.lock"
    sup.jobs_root.mkdir(parents=True, exist_ok=True)
    f = open(lock_path, "w")
    try:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        logger.warning("another supervisor holds %s; exiting", lock_path)
        return
    sup.reap_orphans()
    while True:
        if not sup.drain_once():
            time.sleep(2.0)
