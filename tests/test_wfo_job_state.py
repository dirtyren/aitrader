"""Job state machine tests — file moves between queue/active/history."""
from __future__ import annotations
import json
import os
from pathlib import Path

import pytest

from ui.wfo.job_state import (
    JobRecord, ProgressInfo, JobStatus,
    enqueue, claim_next, mark_running, update_progress,
    finalize, list_jobs, detect_orphans, write_cancel_sentinel,
    has_cancel_sentinel,
)


def _read(path: Path) -> dict:
    return json.loads(path.read_text())


def test_enqueue_writes_queue_file_and_config(tmp_path):
    jobs_root = tmp_path / "jobs"
    payload = {"universe": {"source": "alpaca_scan"}, "windowing": {}, "gate": {}}
    template = {"run": {"output_root": str(tmp_path / "wfo")}, "windowing": {},
                "history": {"start": "2024-01-01", "end": "2025-01-01",
                            "initial_equity": 100000},
                "timeframes": ["15Min"], "gate": {"wfe_min": 0.5,
                "require_positive_oos_pnl": True},
                "fitness": {"min_trades": 20}, "grid": {}, "position_management": {}}
    rec = enqueue(jobs_root, payload=payload, wfo_template=template,
                  job_id="2026-05-21T14-02-11_a3f1")
    assert rec.status == "queued"
    assert rec.job_id == "2026-05-21T14-02-11_a3f1"
    queue_file = jobs_root / "queue" / f"{rec.job_id}.json"
    assert queue_file.exists()
    assert _read(queue_file)["status"] == "queued"
    config_file = Path(rec.wfo_config_path)
    assert config_file.exists()
    import yaml
    written = yaml.safe_load(config_file.read_text())
    assert written["timeframes"] == ["15Min"]


def test_claim_next_moves_oldest_queue_to_active(tmp_path):
    jobs_root = tmp_path / "jobs"
    template = {"run": {"output_root": "x"}, "history": {"start": "x", "end": "x",
                "initial_equity": 1}, "windowing": {}, "timeframes": [],
                "gate": {"wfe_min": 0.5, "require_positive_oos_pnl": True},
                "fitness": {"min_trades": 1}, "grid": {}, "position_management": {}}
    a = enqueue(jobs_root, payload={}, wfo_template=template,
                job_id="2026-05-21T13-00_aa")
    b = enqueue(jobs_root, payload={}, wfo_template=template,
                job_id="2026-05-21T14-00_bb")
    claimed = claim_next(jobs_root)
    assert claimed.job_id == a.job_id
    assert claimed.status == "starting"
    assert not (jobs_root / "queue" / f"{a.job_id}.json").exists()
    assert (jobs_root / "active" / f"{a.job_id}.json").exists()
    assert (jobs_root / "queue" / f"{b.job_id}.json").exists()


def test_claim_next_returns_none_when_queue_empty(tmp_path):
    jobs_root = tmp_path / "jobs"
    (jobs_root / "queue").mkdir(parents=True)
    assert claim_next(jobs_root) is None


def test_mark_running_sets_pid_and_started_at(tmp_path):
    jobs_root = tmp_path / "jobs"
    template = {"run": {"output_root": "x"}, "history": {"start": "x", "end": "x",
                "initial_equity": 1}, "windowing": {}, "timeframes": [],
                "gate": {"wfe_min": 0.5, "require_positive_oos_pnl": True},
                "fitness": {"min_trades": 1}, "grid": {}, "position_management": {}}
    enqueue(jobs_root, payload={}, wfo_template=template, job_id="j1")
    rec = claim_next(jobs_root)
    rec2 = mark_running(jobs_root, rec.job_id, pid=4242)
    assert rec2.status == "running"
    assert rec2.pid == 4242
    assert rec2.started_at is not None
    saved = _read(jobs_root / "active" / "j1.json")
    assert saved["status"] == "running"
    assert saved["pid"] == 4242


def test_update_progress_persists_to_active_file(tmp_path):
    jobs_root = tmp_path / "jobs"
    template = {"run": {"output_root": "x"}, "history": {"start": "x", "end": "x",
                "initial_equity": 1}, "windowing": {}, "timeframes": [],
                "gate": {"wfe_min": 0.5, "require_positive_oos_pnl": True},
                "fitness": {"min_trades": 1}, "grid": {}, "position_management": {}}
    enqueue(jobs_root, payload={}, wfo_template=template, job_id="j1")
    claim_next(jobs_root)
    mark_running(jobs_root, "j1", pid=1)
    update_progress(jobs_root, "j1", ProgressInfo(
        total_pairs=10, completed_pairs=4, current_symbol="AAPL",
        current_timeframe="15Min", rows_written=120, elapsed_s=12.0, eta_s=18.0))
    saved = _read(jobs_root / "active" / "j1.json")
    assert saved["progress"]["completed_pairs"] == 4
    assert saved["progress"]["current_symbol"] == "AAPL"


def test_finalize_moves_active_to_history(tmp_path):
    jobs_root = tmp_path / "jobs"
    template = {"run": {"output_root": "x"}, "history": {"start": "x", "end": "x",
                "initial_equity": 1}, "windowing": {}, "timeframes": [],
                "gate": {"wfe_min": 0.5, "require_positive_oos_pnl": True},
                "fitness": {"min_trades": 1}, "grid": {}, "position_management": {}}
    enqueue(jobs_root, payload={}, wfo_template=template, job_id="j1")
    claim_next(jobs_root)
    mark_running(jobs_root, "j1", pid=1)
    finalize(jobs_root, "j1", status="completed", exit_code=0, error=None)
    assert not (jobs_root / "active" / "j1.json").exists()
    saved = _read(jobs_root / "history" / "j1.json")
    assert saved["status"] == "completed"
    assert saved["exit_code"] == 0
    assert saved["completed_at"] is not None


def test_list_jobs_returns_records_per_directory(tmp_path):
    jobs_root = tmp_path / "jobs"
    template = {"run": {"output_root": "x"}, "history": {"start": "x", "end": "x",
                "initial_equity": 1}, "windowing": {}, "timeframes": [],
                "gate": {"wfe_min": 0.5, "require_positive_oos_pnl": True},
                "fitness": {"min_trades": 1}, "grid": {}, "position_management": {}}
    enqueue(jobs_root, payload={}, wfo_template=template, job_id="j1")
    enqueue(jobs_root, payload={}, wfo_template=template, job_id="j2")
    claim_next(jobs_root)
    mark_running(jobs_root, "j1", pid=1)
    finalize(jobs_root, "j1", status="completed", exit_code=0, error=None)
    queued, active, history = list_jobs(jobs_root)
    assert [r.job_id for r in queued] == ["j2"]
    assert active == []
    assert [r.job_id for r in history] == ["j1"]


def test_detect_orphans_marks_dead_pids(tmp_path):
    jobs_root = tmp_path / "jobs"
    template = {"run": {"output_root": "x"}, "history": {"start": "x", "end": "x",
                "initial_equity": 1}, "windowing": {}, "timeframes": [],
                "gate": {"wfe_min": 0.5, "require_positive_oos_pnl": True},
                "fitness": {"min_trades": 1}, "grid": {}, "position_management": {}}
    enqueue(jobs_root, payload={}, wfo_template=template, job_id="orph")
    claim_next(jobs_root)
    mark_running(jobs_root, "orph", pid=999_999)  # dead PID
    orphans = detect_orphans(jobs_root)
    assert [r.job_id for r in orphans] == ["orph"]
    # detect_orphans does NOT mutate; caller decides whether to finalize
    assert (jobs_root / "active" / "orph.json").exists()


def test_cancel_sentinel(tmp_path):
    jobs_root = tmp_path / "jobs"
    (jobs_root / "active").mkdir(parents=True)
    assert not has_cancel_sentinel(jobs_root, "j1")
    write_cancel_sentinel(jobs_root, "j1")
    assert has_cancel_sentinel(jobs_root, "j1")
