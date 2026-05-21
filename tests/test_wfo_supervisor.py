"""Supervisor: serializes queued jobs through a stubbed subprocess factory."""
from __future__ import annotations
import time
from pathlib import Path

import pytest

from ui.wfo.job_state import enqueue, list_jobs, write_cancel_sentinel
from ui.wfo.supervisor import Supervisor


_TEMPLATE = {
    "run": {"output_root": "runtime/wfo", "parallelism": -1, "random_seed": 42},
    "history": {"start": "2024-01-01", "end": "2024-02-01",
                "initial_equity": 100000},
    "windowing": {"in_sample": "10d", "out_of_sample": "5d", "step": None},
    "universe": {"source": "symbols", "symbols": ["AAPL"]},
    "timeframes": ["15Min"],
    "fitness": {"metric": "sharpe", "min_trades": 1},
    "gate": {"wfe_min": 0.5, "require_positive_oos_pnl": True},
    "grid": {}, "position_management": {},
}


class _StubProc:
    """Minimal subprocess.Popen-shaped stub. The supervisor only needs poll(),
    wait(), terminate(), kill(), pid, and returncode."""
    def __init__(self, *, exit_code: int = 0, run_seconds: float = 0.05):
        self.pid = 11111
        self._exit_code = exit_code
        self._end_at = time.time() + run_seconds
        self.returncode = None
        self.terminated = False
        self.killed = False

    def poll(self):
        if self.returncode is not None:
            return self.returncode
        if time.time() >= self._end_at:
            self.returncode = self._exit_code
            return self.returncode
        return None

    def wait(self, timeout=None):
        deadline = time.time() + (timeout if timeout else 10)
        while time.time() < deadline:
            if self.poll() is not None:
                return self.returncode
            time.sleep(0.01)
        raise TimeoutError

    def terminate(self):
        self.terminated = True
        self._end_at = time.time()  # exit on next poll
        self._exit_code = -15

    def kill(self):
        self.killed = True
        self.returncode = -9


def _factory(exit_code=0, run_seconds=0.05):
    def factory(record):
        return _StubProc(exit_code=exit_code, run_seconds=run_seconds)
    return factory


def test_supervisor_drains_queue_in_order(tmp_path):
    jobs_root = tmp_path / "jobs"
    enqueue(jobs_root, payload={}, wfo_template=_TEMPLATE, job_id="j1")
    enqueue(jobs_root, payload={}, wfo_template=_TEMPLATE, job_id="j2")
    sup = Supervisor(jobs_root, subprocess_factory=_factory(exit_code=0,
                                                             run_seconds=0.02))
    sup.drain_once()  # picks j1
    sup.drain_once()  # picks j2
    queued, active, history = list_jobs(jobs_root)
    assert queued == [] and active == []
    assert sorted(r.job_id for r in history) == ["j1", "j2"]
    assert all(r.status == "completed" for r in history)


def test_supervisor_marks_failure_on_nonzero_exit(tmp_path):
    jobs_root = tmp_path / "jobs"
    enqueue(jobs_root, payload={}, wfo_template=_TEMPLATE, job_id="bad")
    sup = Supervisor(jobs_root,
                     subprocess_factory=_factory(exit_code=2, run_seconds=0.02))
    sup.drain_once()
    _, _, history = list_jobs(jobs_root)
    assert history[0].status == "failed"
    assert history[0].exit_code == 2


def test_supervisor_handles_cancel_sentinel(tmp_path, monkeypatch):
    """When a cancel sentinel exists, supervisor sends terminate() then exits."""
    jobs_root = tmp_path / "jobs"
    enqueue(jobs_root, payload={}, wfo_template=_TEMPLATE, job_id="cancelme")

    proc_holder = {}

    def factory(record):
        proc_holder["proc"] = _StubProc(exit_code=0, run_seconds=10.0)
        return proc_holder["proc"]

    sup = Supervisor(jobs_root, subprocess_factory=factory,
                     poll_interval_s=0.01, cancel_grace_s=0.1)
    # Pre-write the sentinel so the very first poll sees it.
    write_cancel_sentinel(jobs_root, "cancelme")
    sup.drain_once()
    assert proc_holder["proc"].terminated
    _, _, history = list_jobs(jobs_root)
    assert history[0].status == "cancelled"


def test_supervisor_rejects_double_drain(tmp_path):
    """Two drain_once() calls when queue is empty are a no-op (return False)."""
    jobs_root = tmp_path / "jobs"
    sup = Supervisor(jobs_root, subprocess_factory=_factory())
    assert sup.drain_once() is False
    assert sup.drain_once() is False


def test_supervisor_marks_orphans_at_boot(tmp_path):
    """Active job with dead PID gets finalized as failed on boot."""
    from ui.wfo.job_state import claim_next, mark_running
    jobs_root = tmp_path / "jobs"
    enqueue(jobs_root, payload={}, wfo_template=_TEMPLATE, job_id="orph")
    claim_next(jobs_root)
    mark_running(jobs_root, "orph", pid=999_999)
    sup = Supervisor(jobs_root, subprocess_factory=_factory())
    sup.reap_orphans()
    _, active, history = list_jobs(jobs_root)
    assert active == []
    assert history[0].status == "failed"
    assert "orphan" in (history[0].error or "").lower()
