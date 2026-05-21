# WFO Dashboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a Streamlit-driven workflow on top of the existing WFO engine — launch runs, browse results with charts, and approve per-symbol parameter overrides via a candidate→active two-stage flow that replaces the auto-promoted `runtime/wfo/latest` symlink.

**Architecture:** New `ui/wfo/` package (forms, supervisor, approval, charts), new `runtime/wfo/jobs/` orchestration directory + `runtime/wfo/active/live_overrides.yaml`. The WFO engine itself (`backtest/wfo/*`) is unchanged. The CLI loses one line (its symlink update); `main.py` reads the new active path.

**Tech Stack:** Python 3.11, Streamlit 1.41, Plotly 5.24, pyarrow, pandas, joblib, PyYAML. Tests via pytest. Container: docker-compose, dashboard service.

**Spec:** `docs/superpowers/specs/2026-05-21-wfo-dashboard-design.md`

---

## File Structure

**New files:**
- `ui/wfo/__init__.py` — empty marker
- `ui/wfo/job_state.py` — JobRecord/ProgressInfo dataclasses + state-machine helpers
- `ui/wfo/approval.py` — read/write `active/live_overrides.yaml` + audit log
- `ui/wfo/forms.py` — FormPayload dataclass, template merge, Streamlit form rendering
- `ui/wfo/charts.py` — pure data-prep helpers + Plotly figure builders
- `ui/wfo/supervisor.py` — singleton thread, subprocess management
- `ui/wfo/runs_list.py` — Runs table + active-job banner Streamlit panel
- `ui/wfo/run_detail.py` — Run Detail panel (symbol table, charts, approve/reject)
- `ui/wfo/active_panel.py` — Active Overrides panel
- `ui/wfo/tab.py` — top-level WFO-tab entry point + panel switcher
- `tests/test_wfo_job_state.py`
- `tests/test_wfo_active_overrides.py`
- `tests/test_wfo_form_payload.py`
- `tests/test_wfo_charts.py`
- `tests/test_wfo_supervisor.py`

**Modified:**
- `config/settings.yaml` — `overrides.path` → `runtime/wfo/active/live_overrides.yaml`
- `main.py` — none (path comes from settings)
- `scripts/run_wfo.py` — drop the `update_latest_symlink_if_passing(...)` call + import
- `backtest/wfo/report.py` — delete `update_latest_symlink` and `update_latest_symlink_if_passing`
- `tests/test_wfo_report.py` — drop the two tests for those functions
- `tests/test_main_overrides.py` — pin new active path; assert `_provenance` keys ignored
- `ui/dashboard.py` — add WFO tab via `st.tabs([..., "WFO"])`
- `docker-compose.yml` — `dashboard` service: add `env_file: ./config/.env`; remove `TRADING_ENV=test`

---

## Task 1 — Move overrides path from `latest/` to `active/`

**Files:**
- Modify: `config/settings.yaml:131`
- Modify: `tests/test_main_overrides.py` (extend)

- [ ] **Step 1.1: Add a failing test that pins the new default path**

Append to `tests/test_main_overrides.py`:

```python
from pathlib import Path


def test_settings_yaml_overrides_path_points_to_active():
    cfg = yaml.safe_load(Path("config/settings.yaml").read_text())
    assert cfg["overrides"]["path"] == "runtime/wfo/active/live_overrides.yaml"
    assert cfg["overrides"]["enabled"] is True
```

- [ ] **Step 1.2: Run test to verify it fails**

Run: `TRADING_ENV=test pytest tests/test_main_overrides.py::test_settings_yaml_overrides_path_points_to_active -v`
Expected: FAIL — current path is `runtime/wfo/latest/live_overrides.yaml`.

- [ ] **Step 1.3: Update `config/settings.yaml`**

Change line 131:
```yaml
overrides:
  path: runtime/wfo/active/live_overrides.yaml
  enabled: true
```

- [ ] **Step 1.4: Run all override tests — expect pass**

Run: `TRADING_ENV=test pytest tests/test_main_overrides.py -v`
Expected: all pass.

- [ ] **Step 1.5: Commit**

```bash
git add config/settings.yaml tests/test_main_overrides.py
git commit -m "feat(config): point live overrides at runtime/wfo/active/

Replaces the auto-promoted runtime/wfo/latest symlink contract — the
dashboard will manage runtime/wfo/active/live_overrides.yaml as the
single live-engine source of truth."
```

---

## Task 2 — Make `apply_overrides` tolerant of `_provenance` metadata

**Files:**
- Test: `tests/test_main_overrides.py` (extend)

The downstream consumers (`_build_setups_from_override`, `position_manager_for`, `timeframe_for`) already access named keys (`setup`, `setup_params`, `position_management`, `timeframe`) and ignore unknown keys. This task adds a regression test pinning that contract.

- [ ] **Step 2.1: Add failing test**

Append to `tests/test_main_overrides.py`:

```python
def test_apply_overrides_tolerates_provenance_keys(tmp_path):
    """The dashboard writes a `_provenance` block per symbol; apply_overrides
    must keep working — downstream consumers read named keys only."""
    overrides_path = tmp_path / "live_overrides.yaml"
    overrides_path.write_text(yaml.safe_dump({
        "symbols": {
            "AAPL": {
                "timeframe": "15Min",
                "setup": "price_discovery",
                "setup_params": {"atr_mult_stop": 1.25, "target_R": 2.0,
                                 "arm_window_bars": 6},
                "position_management": {"max_hold_bars": 12, "breakeven_at_R": 1.0},
                "metadata": {"walks": 30, "wfe": 0.78},
                "_provenance": {
                    "run_id": "2026-05-21T14-02_a3f1c2",
                    "approved_at": "2026-05-21T15:11:08Z",
                    "approved_by": "dashboard",
                },
            },
        },
    }))
    cfg = {"setups": {}}
    out = apply_overrides(cfg, str(overrides_path))
    assert "AAPL" in out["_per_symbol_overrides"]
    entry = out["_per_symbol_overrides"]["AAPL"]
    assert entry["setup"] == "price_discovery"
    assert "_provenance" in entry  # passed through, not stripped
```

- [ ] **Step 2.2: Run test — expect PASS already (regression lock)**

Run: `TRADING_ENV=test pytest tests/test_main_overrides.py::test_apply_overrides_tolerates_provenance_keys -v`
Expected: PASS (current implementation tolerates extra keys).

- [ ] **Step 2.3: Commit**

```bash
git add tests/test_main_overrides.py
git commit -m "test(main): pin tolerance of _provenance keys in overrides"
```

---

## Task 3 — Remove `update_latest_symlink` from CLI and report module

**Files:**
- Modify: `scripts/run_wfo.py`
- Modify: `backtest/wfo/report.py`
- Modify: `tests/test_wfo_report.py`

- [ ] **Step 3.1: Add failing test asserting the import is gone**

Append to `tests/test_wfo_report.py`:

```python
def test_update_latest_symlink_helpers_removed():
    """The dashboard owns promotion now — these helpers are dead code."""
    import backtest.wfo.report as report
    assert not hasattr(report, "update_latest_symlink")
    assert not hasattr(report, "update_latest_symlink_if_passing")


def test_run_wfo_does_not_import_symlink_helper():
    import scripts.run_wfo as cli
    assert "update_latest_symlink_if_passing" not in dir(cli)
```

- [ ] **Step 3.2: Run tests — expect FAIL**

Run: `TRADING_ENV=test pytest tests/test_wfo_report.py::test_update_latest_symlink_helpers_removed tests/test_wfo_report.py::test_run_wfo_does_not_import_symlink_helper -v`
Expected: both FAIL.

- [ ] **Step 3.3: Delete the two existing symlink tests**

In `tests/test_wfo_report.py`, remove the two test functions starting at lines 163 and 180:
- `test_update_latest_symlink_atomic`
- `test_update_latest_symlink_skipped_when_zero_passed`

- [ ] **Step 3.4: Delete the two helper functions in `backtest/wfo/report.py`**

Remove `update_latest_symlink` and `update_latest_symlink_if_passing` (the block from line 160 to end of `update_latest_symlink_if_passing`). Also remove the now-unused `import os` if no other uses remain in the file.

- [ ] **Step 3.5: Update `scripts/run_wfo.py`**

In the import block at top, remove `update_latest_symlink_if_passing` from the `from backtest.wfo.report import (...)` line:

```python
from backtest.wfo.report import (
    GateConfig, aggregate_results, emit_live_overrides, emit_summary_md,
)
```

Remove the symlink-related lines (around line 164–166):

Delete:
```python
    latest = Path(wfo_cfg["run"]["output_root"]) / "latest"
    update_latest_symlink_if_passing(latest, output_dir, aggregated)
```

- [ ] **Step 3.6: Run all WFO tests — expect pass**

Run: `TRADING_ENV=test pytest tests/test_wfo_report.py tests/test_wfo_runner.py -v`
Expected: all pass; the two new "removed" tests pass.

- [ ] **Step 3.7: Commit**

```bash
git add scripts/run_wfo.py backtest/wfo/report.py tests/test_wfo_report.py
git commit -m "refactor(wfo): drop runtime/wfo/latest symlink update from CLI

The dashboard now owns promotion via a candidate->active two-stage flow.
runtime/wfo/<run_id>/live_overrides.yaml is the immutable candidate;
runtime/wfo/active/live_overrides.yaml is the dashboard-managed live source."
```

---

## Task 4 — Job state data model + state-machine helpers

**Files:**
- Create: `ui/wfo/__init__.py`
- Create: `ui/wfo/job_state.py`
- Test: `tests/test_wfo_job_state.py`

- [ ] **Step 4.1: Create the package marker**

Write `ui/wfo/__init__.py`:

```python
"""WFO dashboard surface: forms, supervisor, approval, charts, panels."""
```

- [ ] **Step 4.2: Write the failing tests**

Write `tests/test_wfo_job_state.py`:

```python
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
```

- [ ] **Step 4.3: Run tests to verify they all fail**

Run: `pytest tests/test_wfo_job_state.py -v`
Expected: all FAIL with `ModuleNotFoundError: No module named 'ui.wfo.job_state'`.

- [ ] **Step 4.4: Implement `ui/wfo/job_state.py`**

```python
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
import os
from dataclasses import dataclass, asdict, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

import yaml

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
    _ensure_dirs(jobs_root)
    if job_id is None:
        job_id = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S_%f")[:-3]
    config = _merge_payload_into_template(payload, wfo_template)
    config_path = jobs_root / "configs" / f"{job_id}.yaml"
    config_path.write_text(yaml.safe_dump(config, sort_keys=False))
    rec = JobRecord(
        job_id=job_id, run_id=_build_run_id(job_id), status="queued",
        queued_at=_now_iso(), form_payload=payload,
        wfo_config_path=str(config_path),
    )
    _write_atomic(_path_for(jobs_root, "queue", job_id), rec.to_dict())
    return rec


def _merge_payload_into_template(payload: dict, template: dict) -> dict:
    """Apply payload over a deep-copied template. Real merge logic lives in
    ui/wfo/forms.py; this is a thin shim so enqueue can be called with raw
    template + payload in tests."""
    import copy
    out = copy.deepcopy(template)
    out.setdefault("universe", {}).update(payload.get("universe", {}))
    out.setdefault("windowing", {}).update(payload.get("windowing", {}))
    out.setdefault("gate", {}).update(payload.get("gate", {}))
    if "history" in payload:
        out["history"].update(payload["history"])
    if "timeframes" in payload:
        out["timeframes"] = payload["timeframes"]
    if "fitness" in payload:
        out.setdefault("fitness", {}).update(payload["fitness"])
    return out


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
    return rec


def mark_running(jobs_root: Path, job_id: str, *, pid: int) -> JobRecord:
    path = _path_for(jobs_root, "active", job_id)
    rec = _read_record(path)
    rec = replace(rec, status="running", pid=pid, started_at=_now_iso())
    _write_atomic(path, rec.to_dict())
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


def has_cancel_sentinel(jobs_root: Path, job_id: str) -> bool:
    return (jobs_root / "active" / f"{job_id}.cancel").exists()
```

- [ ] **Step 4.5: Run tests — expect pass**

Run: `pytest tests/test_wfo_job_state.py -v`
Expected: all 9 tests pass.

- [ ] **Step 4.6: Run full suite to confirm no regressions**

Run: `TRADING_ENV=test pytest -x`
Expected: all pass.

- [ ] **Step 4.7: Commit**

```bash
git add ui/wfo/__init__.py ui/wfo/job_state.py tests/test_wfo_job_state.py
git commit -m "feat(wfo): job state machine + queue/active/history transitions"
```

---

## Task 5 — Active overrides read/write + audit log

**Files:**
- Create: `ui/wfo/approval.py`
- Test: `tests/test_wfo_active_overrides.py`

- [ ] **Step 5.1: Write failing tests**

Write `tests/test_wfo_active_overrides.py`:

```python
"""Approval module — atomic active-overrides writes and audit log."""
from __future__ import annotations
import json
from pathlib import Path

import pytest
import yaml

from ui.wfo.approval import (
    read_active, approve_symbol, revert_symbol, read_audit_tail,
    GateFailedApproveError,
)


def _candidate_entry(setup="price_discovery", passed=True) -> dict:
    return {
        "timeframe": "15Min", "setup": setup,
        "setup_params": {"atr_mult_stop": 1.25, "target_R": 2.0,
                         "arm_window_bars": 6},
        "position_management": {"max_hold_bars": 12, "breakeven_at_R": 1.0},
        "metadata": {"walks": 30, "wfe": 0.78, "total_oos_pnl": 4213.50,
                     "passed": passed},
    }


def test_read_active_returns_empty_when_missing(tmp_path):
    assert read_active(tmp_path / "missing.yaml") == {"symbols": {}}


def test_approve_symbol_creates_file_with_provenance(tmp_path):
    active = tmp_path / "active.yaml"
    audit = tmp_path / "audit.jsonl"
    approve_symbol(active_path=active, audit_path=audit,
                   symbol="AAPL", candidate=_candidate_entry(),
                   run_id="run_a")
    payload = yaml.safe_load(active.read_text())
    assert "AAPL" in payload["symbols"]
    entry = payload["symbols"]["AAPL"]
    assert entry["setup"] == "price_discovery"
    assert entry["_provenance"]["run_id"] == "run_a"
    assert entry["_provenance"]["approved_by"] == "dashboard"


def test_approve_symbol_preserves_other_symbols(tmp_path):
    active = tmp_path / "active.yaml"
    audit = tmp_path / "audit.jsonl"
    approve_symbol(active, audit, "AAPL", _candidate_entry(), "run_a")
    approve_symbol(active, audit, "TSLA", _candidate_entry(setup="vwap_bounce"),
                   "run_b")
    payload = yaml.safe_load(active.read_text())
    assert set(payload["symbols"].keys()) == {"AAPL", "TSLA"}
    assert payload["symbols"]["TSLA"]["setup"] == "vwap_bounce"


def test_approve_symbol_replaces_prior_entry(tmp_path):
    active = tmp_path / "active.yaml"
    audit = tmp_path / "audit.jsonl"
    approve_symbol(active, audit, "AAPL", _candidate_entry(), "run_a")
    second = _candidate_entry()
    second["setup_params"]["atr_mult_stop"] = 1.50
    approve_symbol(active, audit, "AAPL", second, "run_b")
    payload = yaml.safe_load(active.read_text())
    assert payload["symbols"]["AAPL"]["setup_params"]["atr_mult_stop"] == 1.50
    assert payload["symbols"]["AAPL"]["_provenance"]["run_id"] == "run_b"


def test_approve_gate_failed_raises(tmp_path):
    active = tmp_path / "active.yaml"
    audit = tmp_path / "audit.jsonl"
    with pytest.raises(GateFailedApproveError):
        approve_symbol(active, audit, "AAPL",
                       _candidate_entry(passed=False), "run_a")
    assert not active.exists()


def test_revert_removes_symbol_only(tmp_path):
    active = tmp_path / "active.yaml"
    audit = tmp_path / "audit.jsonl"
    approve_symbol(active, audit, "AAPL", _candidate_entry(), "run_a")
    approve_symbol(active, audit, "TSLA", _candidate_entry(), "run_a")
    revert_symbol(active, audit, "AAPL")
    payload = yaml.safe_load(active.read_text())
    assert list(payload["symbols"].keys()) == ["TSLA"]


def test_revert_missing_symbol_is_idempotent(tmp_path):
    active = tmp_path / "active.yaml"
    audit = tmp_path / "audit.jsonl"
    revert_symbol(active, audit, "AAPL")  # no error
    payload = yaml.safe_load(active.read_text())
    assert payload["symbols"] == {}


def test_audit_log_records_each_action(tmp_path):
    active = tmp_path / "active.yaml"
    audit = tmp_path / "audit.jsonl"
    approve_symbol(active, audit, "AAPL", _candidate_entry(), "run_a")
    revert_symbol(active, audit, "AAPL")
    lines = audit.read_text().strip().splitlines()
    assert len(lines) == 2
    a, b = json.loads(lines[0]), json.loads(lines[1])
    assert a["action"] == "approve" and a["symbol"] == "AAPL"
    assert a["run_id"] == "run_a"
    assert b["action"] == "revert" and b["symbol"] == "AAPL"


def test_read_audit_tail_returns_last_n(tmp_path):
    active = tmp_path / "active.yaml"
    audit = tmp_path / "audit.jsonl"
    for i in range(5):
        approve_symbol(active, audit, f"S{i}", _candidate_entry(), "run_a")
    tail = read_audit_tail(audit, n=3)
    assert [t["symbol"] for t in tail] == ["S2", "S3", "S4"]
```

- [ ] **Step 5.2: Run — expect FAIL**

Run: `pytest tests/test_wfo_active_overrides.py -v`
Expected: all FAIL on import.

- [ ] **Step 5.3: Implement `ui/wfo/approval.py`**

```python
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
```

- [ ] **Step 5.4: Run tests — expect pass**

Run: `pytest tests/test_wfo_active_overrides.py -v`
Expected: all 9 tests pass.

- [ ] **Step 5.5: Commit**

```bash
git add ui/wfo/approval.py tests/test_wfo_active_overrides.py
git commit -m "feat(wfo): per-symbol approve/revert with atomic write + audit log"
```

---

## Task 6 — WFO form payload + template merge

**Files:**
- Create: `ui/wfo/forms.py` (data layer only — UI rendering added in Task 10)
- Test: `tests/test_wfo_form_payload.py`

- [ ] **Step 6.1: Write failing tests**

Write `tests/test_wfo_form_payload.py`:

```python
"""Form payload merge into the canonical wfo.yaml template."""
from __future__ import annotations
import copy

import pytest

from ui.wfo.forms import FormPayload, UniverseSpec, WindowingSpec, GateSpec, \
    merge_payload_into_template, validate_form, FormValidationError


_TEMPLATE = {
    "run": {"output_root": "runtime/wfo", "parallelism": -1, "random_seed": 42},
    "history": {"start": "2024-01-01", "end": "2026-04-30",
                "initial_equity": 100_000},
    "windowing": {"in_sample": "6mo", "out_of_sample": "1mo", "step": None},
    "universe": {"source": "alpaca_scan",
                 "alpaca_scan": {"classes": ["us_equity"],
                                 "min_dollar_volume_20d": 5_000_000,
                                 "top_n_per_class": {"us_equity": 100},
                                 "cache_dir": "runtime/wfo/universe_cache"}},
    "timeframes": ["5Min", "15Min", "30Min", "1Hour"],
    "fitness": {"metric": "sharpe", "min_trades": 20},
    "gate": {"wfe_min": 0.5, "require_positive_oos_pnl": True},
    "grid": {"price_discovery": {"enabled": [True]}},
    "position_management": {"max_hold_bars": [12]},
}


def _payload(**kw) -> FormPayload:
    base = FormPayload(
        universe=UniverseSpec(source="alpaca_scan", classes=["us_equity"],
                              min_dollar_volume_20d=5_000_000,
                              top_n_per_class={"us_equity": 100},
                              allowlist=[], blocklist=[], explicit_symbols=[]),
        windowing=WindowingSpec(in_sample="6mo", out_of_sample="1mo", step=None,
                                history_start="2024-01-01",
                                history_end="2026-04-30",
                                timeframes=["15Min"]),
        gate=GateSpec(wfe_min=0.5, require_positive_oos_pnl=True, min_trades=20),
    )
    return base


def test_merge_does_not_mutate_template():
    template = copy.deepcopy(_TEMPLATE)
    merge_payload_into_template(_payload(), template)
    assert template == _TEMPLATE


def test_merge_overrides_universe_top_n():
    p = _payload()
    p.universe.top_n_per_class = {"us_equity": 50, "crypto": None}
    p.universe.classes = ["us_equity", "crypto"]
    out = merge_payload_into_template(p, _TEMPLATE)
    scan = out["universe"]["alpaca_scan"]
    assert scan["top_n_per_class"] == {"us_equity": 50, "crypto": None}
    assert scan["classes"] == ["us_equity", "crypto"]


def test_merge_explicit_symbols_mode():
    p = _payload()
    p.universe.source = "symbols"
    p.universe.explicit_symbols = ["AAPL", "TSLA"]
    out = merge_payload_into_template(p, _TEMPLATE)
    assert out["universe"]["source"] == "symbols"
    assert out["universe"]["symbols"] == ["AAPL", "TSLA"]


def test_merge_windowing_and_timeframes():
    p = _payload()
    p.windowing.in_sample = "9mo"
    p.windowing.out_of_sample = "2mo"
    p.windowing.timeframes = ["5Min", "15Min"]
    p.windowing.history_start = "2025-01-01"
    out = merge_payload_into_template(p, _TEMPLATE)
    assert out["windowing"]["in_sample"] == "9mo"
    assert out["windowing"]["out_of_sample"] == "2mo"
    assert out["timeframes"] == ["5Min", "15Min"]
    assert out["history"]["start"] == "2025-01-01"


def test_merge_gate_overlay():
    p = _payload()
    p.gate.wfe_min = 0.7
    p.gate.require_positive_oos_pnl = False
    p.gate.min_trades = 30
    out = merge_payload_into_template(p, _TEMPLATE)
    assert out["gate"]["wfe_min"] == 0.7
    assert out["gate"]["require_positive_oos_pnl"] is False
    assert out["fitness"]["min_trades"] == 30


def test_validate_alpaca_scan_requires_at_least_one_class():
    p = _payload()
    p.universe.classes = []
    with pytest.raises(FormValidationError, match="classes"):
        validate_form(p)


def test_validate_explicit_symbols_requires_at_least_one():
    p = _payload()
    p.universe.source = "symbols"
    p.universe.explicit_symbols = []
    with pytest.raises(FormValidationError, match="symbols"):
        validate_form(p)


def test_validate_timeframes_required():
    p = _payload()
    p.windowing.timeframes = []
    with pytest.raises(FormValidationError, match="timeframes"):
        validate_form(p)


def test_validate_window_format():
    p = _payload()
    p.windowing.in_sample = "weeks"
    with pytest.raises(FormValidationError, match="in_sample"):
        validate_form(p)
```

- [ ] **Step 6.2: Run — expect FAIL**

Run: `pytest tests/test_wfo_form_payload.py -v`
Expected: all FAIL on import.

- [ ] **Step 6.3: Implement `ui/wfo/forms.py` (data layer)**

```python
"""WFO New Run form: dataclasses, validation, payload→template merge.

UI rendering (Streamlit widgets) is added later in this file under render_form().
"""
from __future__ import annotations
import copy
import re
from dataclasses import dataclass, field

_DURATION_RE = re.compile(r"^\d+(d|mo)$")


class FormValidationError(ValueError):
    pass


@dataclass
class UniverseSpec:
    source: str  # "alpaca_scan" | "symbols"
    classes: list[str] = field(default_factory=list)
    min_dollar_volume_20d: float = 5_000_000
    top_n_per_class: dict[str, int | None] = field(default_factory=dict)
    allowlist: list[str] = field(default_factory=list)
    blocklist: list[str] = field(default_factory=list)
    explicit_symbols: list[str] = field(default_factory=list)


@dataclass
class WindowingSpec:
    in_sample: str = "6mo"
    out_of_sample: str = "1mo"
    step: str | None = None
    history_start: str = "2024-01-01"
    history_end: str = "2026-04-30"
    timeframes: list[str] = field(default_factory=list)


@dataclass
class GateSpec:
    wfe_min: float = 0.5
    require_positive_oos_pnl: bool = True
    min_trades: int = 20


@dataclass
class FormPayload:
    universe: UniverseSpec
    windowing: WindowingSpec
    gate: GateSpec


def validate_form(p: FormPayload) -> None:
    if p.universe.source == "alpaca_scan":
        if not p.universe.classes:
            raise FormValidationError(
                "Universe: at least one asset class is required for alpaca_scan")
    elif p.universe.source == "symbols":
        if not p.universe.explicit_symbols:
            raise FormValidationError(
                "Universe: at least one explicit symbol is required")
    else:
        raise FormValidationError(
            f"Universe: unknown source {p.universe.source!r}")

    if not p.windowing.timeframes:
        raise FormValidationError("Windowing: at least one timeframe required")
    for k in ("in_sample", "out_of_sample"):
        v = getattr(p.windowing, k)
        if not _DURATION_RE.match(v):
            raise FormValidationError(
                f"Windowing: {k} must match <int>(d|mo), got {v!r}")
    if p.windowing.step is not None and not _DURATION_RE.match(p.windowing.step):
        raise FormValidationError("Windowing: step must be blank or <int>(d|mo)")

    if not (0.0 < p.gate.wfe_min <= 5.0):
        raise FormValidationError("Gate: wfe_min must be in (0, 5]")
    if p.gate.min_trades < 1:
        raise FormValidationError("Gate: min_trades must be >= 1")


def merge_payload_into_template(p: FormPayload, template: dict) -> dict:
    out = copy.deepcopy(template)

    if p.universe.source == "alpaca_scan":
        scan = out.setdefault("universe", {}).setdefault("alpaca_scan", {})
        scan["classes"] = list(p.universe.classes)
        scan["min_dollar_volume_20d"] = p.universe.min_dollar_volume_20d
        scan["top_n_per_class"] = dict(p.universe.top_n_per_class)
        if p.universe.allowlist:
            scan["allowlist"] = list(p.universe.allowlist)
        if p.universe.blocklist:
            scan["blocklist"] = list(p.universe.blocklist)
        out["universe"]["source"] = "alpaca_scan"
    else:
        out["universe"] = {"source": "symbols",
                           "symbols": list(p.universe.explicit_symbols)}

    win = out.setdefault("windowing", {})
    win["in_sample"] = p.windowing.in_sample
    win["out_of_sample"] = p.windowing.out_of_sample
    win["step"] = p.windowing.step

    out.setdefault("history", {})
    out["history"]["start"] = p.windowing.history_start
    out["history"]["end"] = p.windowing.history_end

    out["timeframes"] = list(p.windowing.timeframes)

    out.setdefault("gate", {})
    out["gate"]["wfe_min"] = p.gate.wfe_min
    out["gate"]["require_positive_oos_pnl"] = p.gate.require_positive_oos_pnl

    out.setdefault("fitness", {})
    out["fitness"]["min_trades"] = p.gate.min_trades

    return out
```

- [ ] **Step 6.4: Run tests — expect pass**

Run: `pytest tests/test_wfo_form_payload.py -v`
Expected: all 9 tests pass.

- [ ] **Step 6.5: Commit**

```bash
git add ui/wfo/forms.py tests/test_wfo_form_payload.py
git commit -m "feat(wfo): form-payload dataclasses + validation + template merge"
```

---

## Task 7 — Chart data-prep helpers

**Files:**
- Create: `ui/wfo/charts.py`
- Test: `tests/test_wfo_charts.py`

- [ ] **Step 7.1: Write failing tests**

Write `tests/test_wfo_charts.py`:

```python
"""Chart data-prep helpers — pure DataFrame transforms over results.parquet."""
from __future__ import annotations
import math

import pandas as pd
import pytest

from ui.wfo.charts import (
    walk_oos_curve, walk_oos_sharpe_bars, is_vs_oos_scatter,
    param_heatmap, pick_heatmap_axes,
)


def _row(symbol="AAPL", timeframe="15Min", setup="price_discovery",
         walk_idx=0, fingerprint="fp1", combo_values_json='{}',
         is_sharpe=1.0, is_trades=25, is_pnl=100.0,
         oos_sharpe=0.8, oos_trades=10, oos_pnl=50.0,
         oos_max_dd=-20.0, oos_avg_R=0.5, status="ok", error=None,
         asset_class="us_equity") -> dict:
    return locals().copy()


def _df(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(rows)


def test_walk_oos_curve_picks_is_winners_per_walk():
    """For each walk, the IS-best combo wins; OOS pnl from that row is plotted."""
    rows = [
        _row(walk_idx=0, fingerprint="a", is_sharpe=1.0, oos_pnl=100.0),
        _row(walk_idx=0, fingerprint="b", is_sharpe=2.0, oos_pnl=20.0),
        _row(walk_idx=1, fingerprint="a", is_sharpe=1.5, oos_pnl=10.0),
        _row(walk_idx=1, fingerprint="b", is_sharpe=0.5, oos_pnl=80.0),
    ]
    df = walk_oos_curve(_df(rows), "AAPL", "15Min", "price_discovery")
    assert df["walk_idx"].tolist() == [0, 1]
    assert df["oos_pnl"].tolist() == [20.0, 10.0]
    assert df["cumulative_oos_pnl"].tolist() == [20.0, 30.0]


def test_walk_oos_sharpe_bars():
    rows = [
        _row(walk_idx=0, fingerprint="a", is_sharpe=2.0, oos_sharpe=0.7),
        _row(walk_idx=0, fingerprint="b", is_sharpe=1.0, oos_sharpe=2.0),
        _row(walk_idx=1, fingerprint="a", is_sharpe=1.0, oos_sharpe=-0.5),
    ]
    df = walk_oos_sharpe_bars(_df(rows), "AAPL", "15Min", "price_discovery")
    assert df["walk_idx"].tolist() == [0, 1]
    assert df["oos_sharpe"].tolist() == [0.7, -0.5]


def test_is_vs_oos_scatter_one_point_per_walk():
    rows = [
        _row(walk_idx=0, fingerprint="a", is_sharpe=2.0, oos_sharpe=0.7),
        _row(walk_idx=0, fingerprint="b", is_sharpe=1.0, oos_sharpe=2.5),
        _row(walk_idx=1, fingerprint="a", is_sharpe=1.5, oos_sharpe=-0.3),
    ]
    df = is_vs_oos_scatter(_df(rows), "AAPL", "15Min", "price_discovery")
    assert df["walk_idx"].tolist() == [0, 1]
    assert df["is_sharpe"].tolist() == [2.0, 1.5]
    assert df["oos_sharpe"].tolist() == [0.7, -0.3]


def test_param_heatmap_means_oos_sharpe_per_param_pair():
    rows = [
        _row(walk_idx=0, fingerprint="a", oos_sharpe=1.0,
             combo_values_json='{"atr_mult_stop": 1.0, "target_R": 1.5}'),
        _row(walk_idx=1, fingerprint="a", oos_sharpe=0.0,
             combo_values_json='{"atr_mult_stop": 1.0, "target_R": 1.5}'),
        _row(walk_idx=0, fingerprint="b", oos_sharpe=2.0,
             combo_values_json='{"atr_mult_stop": 1.0, "target_R": 2.0}'),
    ]
    df = param_heatmap(_df(rows), "AAPL", "15Min", "price_discovery",
                       axes=("atr_mult_stop", "target_R"))
    cell = df[(df["atr_mult_stop"] == 1.0) & (df["target_R"] == 1.5)]
    assert cell["mean_oos_sharpe"].iloc[0] == pytest.approx(0.5)
    cell2 = df[(df["atr_mult_stop"] == 1.0) & (df["target_R"] == 2.0)]
    assert cell2["mean_oos_sharpe"].iloc[0] == pytest.approx(2.0)


def test_pick_heatmap_axes_per_setup():
    assert pick_heatmap_axes("price_discovery") == ("atr_mult_stop", "target_R")
    assert pick_heatmap_axes("vwap_bounce") == ("atr_mult_stop", "target_R")
    assert pick_heatmap_axes("fade_extreme") == ("atr_mult_stop", "max_hold_bars")
    assert pick_heatmap_axes("return_to_value") == ("atr_mult_stop",
                                                     "arm_window_bars")


def test_walk_oos_curve_filters_failed_status():
    rows = [
        _row(walk_idx=0, fingerprint="a", is_sharpe=1.0, oos_pnl=100.0),
        _row(walk_idx=0, fingerprint="b", is_sharpe=5.0, oos_pnl=999.0,
             status="failed"),
    ]
    df = walk_oos_curve(_df(rows), "AAPL", "15Min", "price_discovery")
    # The failed row would have won by IS sharpe — but it's filtered out.
    assert df["oos_pnl"].tolist() == [100.0]


def test_helpers_return_empty_for_unknown_symbol():
    rows = [_row()]
    out = walk_oos_curve(_df(rows), "ZZZ", "15Min", "price_discovery")
    assert out.empty
    assert list(out.columns) == ["walk_idx", "oos_pnl", "cumulative_oos_pnl"]
```

- [ ] **Step 7.2: Run — expect FAIL**

Run: `pytest tests/test_wfo_charts.py -v`
Expected: all FAIL on import.

- [ ] **Step 7.3: Implement `ui/wfo/charts.py`**

```python
"""WFO chart helpers: pure data prep + Plotly figure builders.

Data-prep functions take the full results.parquet DataFrame and return
chart-ready DataFrames. Figure builders (build_*_fig) wrap them in Plotly.
"""
from __future__ import annotations
import json

import pandas as pd

_HEATMAP_AXES = {
    "price_discovery":  ("atr_mult_stop", "target_R"),
    "vwap_bounce":      ("atr_mult_stop", "target_R"),
    "fade_extreme":     ("atr_mult_stop", "max_hold_bars"),
    "return_to_value":  ("atr_mult_stop", "arm_window_bars"),
}


def pick_heatmap_axes(setup: str) -> tuple[str, str]:
    return _HEATMAP_AXES.get(setup, ("atr_mult_stop", "target_R"))


def _filter(df: pd.DataFrame, symbol: str, timeframe: str, setup: str) -> pd.DataFrame:
    return df[(df["symbol"] == symbol)
              & (df["timeframe"] == timeframe)
              & (df["setup"] == setup)
              & (df["status"] == "ok")]


def _is_winners(df: pd.DataFrame) -> pd.DataFrame:
    """Per walk_idx, return the row with max is_sharpe."""
    if df.empty:
        return df
    idx = df.groupby("walk_idx")["is_sharpe"].idxmax()
    return df.loc[idx].sort_values("walk_idx").reset_index(drop=True)


def walk_oos_curve(df: pd.DataFrame, symbol: str, timeframe: str,
                   setup: str) -> pd.DataFrame:
    sub = _is_winners(_filter(df, symbol, timeframe, setup))
    out_cols = ["walk_idx", "oos_pnl", "cumulative_oos_pnl"]
    if sub.empty:
        return pd.DataFrame(columns=out_cols)
    out = sub[["walk_idx", "oos_pnl"]].copy()
    out["cumulative_oos_pnl"] = out["oos_pnl"].cumsum()
    return out.reset_index(drop=True)


def walk_oos_sharpe_bars(df: pd.DataFrame, symbol: str, timeframe: str,
                         setup: str) -> pd.DataFrame:
    sub = _is_winners(_filter(df, symbol, timeframe, setup))
    out_cols = ["walk_idx", "oos_sharpe"]
    if sub.empty:
        return pd.DataFrame(columns=out_cols)
    return sub[out_cols].reset_index(drop=True)


def is_vs_oos_scatter(df: pd.DataFrame, symbol: str, timeframe: str,
                      setup: str) -> pd.DataFrame:
    sub = _is_winners(_filter(df, symbol, timeframe, setup))
    out_cols = ["walk_idx", "is_sharpe", "oos_sharpe"]
    if sub.empty:
        return pd.DataFrame(columns=out_cols)
    return sub[out_cols].reset_index(drop=True)


def param_heatmap(df: pd.DataFrame, symbol: str, timeframe: str, setup: str,
                  axes: tuple[str, str]) -> pd.DataFrame:
    sub = _filter(df, symbol, timeframe, setup)
    out_cols = [axes[0], axes[1], "mean_oos_sharpe"]
    if sub.empty:
        return pd.DataFrame(columns=out_cols)
    parsed = sub["combo_values_json"].apply(json.loads)
    df2 = sub.assign(**{axes[0]: parsed.apply(lambda d: d.get(axes[0])),
                        axes[1]: parsed.apply(lambda d: d.get(axes[1]))})
    df2 = df2.dropna(subset=[axes[0], axes[1]])
    grouped = (df2.groupby([axes[0], axes[1]])["oos_sharpe"]
                  .mean()
                  .reset_index()
                  .rename(columns={"oos_sharpe": "mean_oos_sharpe"}))
    return grouped


# --- Plotly figure builders -------------------------------------------------

def build_equity_fig(df: pd.DataFrame, symbol: str):
    import plotly.graph_objects as go
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=df["walk_idx"], y=df["cumulative_oos_pnl"],
                             mode="lines+markers", name="Cumulative OOS P&L"))
    fig.update_layout(title=f"{symbol} — OOS equity (walk-stitched)",
                      xaxis_title="walk", yaxis_title="cumulative OOS P&L")
    return fig


def build_sharpe_bars_fig(df: pd.DataFrame, symbol: str):
    import plotly.graph_objects as go
    fig = go.Figure()
    fig.add_trace(go.Bar(x=df["walk_idx"], y=df["oos_sharpe"],
                         name="OOS Sharpe"))
    fig.update_layout(title=f"{symbol} — per-walk OOS Sharpe",
                      xaxis_title="walk", yaxis_title="OOS Sharpe")
    return fig


def build_is_oos_scatter_fig(df: pd.DataFrame, symbol: str):
    import plotly.graph_objects as go
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=df["is_sharpe"], y=df["oos_sharpe"],
                             mode="markers", text=df["walk_idx"], name="walks"))
    if not df.empty:
        lo = float(min(df["is_sharpe"].min(), df["oos_sharpe"].min()))
        hi = float(max(df["is_sharpe"].max(), df["oos_sharpe"].max()))
        fig.add_trace(go.Scatter(x=[lo, hi], y=[lo, hi], mode="lines",
                                 name="y=x", line=dict(dash="dash")))
    fig.update_layout(title=f"{symbol} — IS vs OOS Sharpe",
                      xaxis_title="IS Sharpe", yaxis_title="OOS Sharpe")
    return fig


def build_heatmap_fig(df: pd.DataFrame, symbol: str, axes: tuple[str, str]):
    import plotly.graph_objects as go
    if df.empty:
        return go.Figure().update_layout(title=f"{symbol} — no data")
    pivot = df.pivot(index=axes[1], columns=axes[0], values="mean_oos_sharpe")
    fig = go.Figure(data=go.Heatmap(
        z=pivot.values, x=pivot.columns, y=pivot.index,
        colorbar=dict(title="mean OOS Sharpe")))
    fig.update_layout(title=f"{symbol} — param heatmap ({axes[0]} × {axes[1]})",
                      xaxis_title=axes[0], yaxis_title=axes[1])
    return fig
```

- [ ] **Step 7.4: Run tests — expect pass**

Run: `pytest tests/test_wfo_charts.py -v`
Expected: all 7 tests pass.

- [ ] **Step 7.5: Commit**

```bash
git add ui/wfo/charts.py tests/test_wfo_charts.py
git commit -m "feat(wfo): chart data-prep helpers + Plotly figure builders"
```

---

## Task 8 — Job supervisor

**Files:**
- Create: `ui/wfo/supervisor.py`
- Test: `tests/test_wfo_supervisor.py`

- [ ] **Step 8.1: Write failing tests**

Write `tests/test_wfo_supervisor.py`:

```python
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
```

- [ ] **Step 8.2: Run — expect FAIL**

Run: `pytest tests/test_wfo_supervisor.py -v`
Expected: all FAIL on import.

- [ ] **Step 8.3: Implement `ui/wfo/supervisor.py`**

```python
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
```

- [ ] **Step 8.4: Run tests — expect pass**

Run: `pytest tests/test_wfo_supervisor.py -v`
Expected: all 5 tests pass.

- [ ] **Step 8.5: Run full suite to confirm no regressions**

Run: `TRADING_ENV=test pytest -x`
Expected: all pass.

- [ ] **Step 8.6: Commit**

```bash
git add ui/wfo/supervisor.py tests/test_wfo_supervisor.py
git commit -m "feat(wfo): job supervisor with subprocess management + cancel + orphan reap"
```

---

## Task 9 — WFO tab: Runs list panel

**Files:**
- Create: `ui/wfo/runs_list.py`

This task renders Streamlit widgets — no unit tests (manual smoke per spec §6.3).

- [ ] **Step 9.1: Implement `ui/wfo/runs_list.py`**

```python
"""Runs-list panel: active-job banner, queue list, completed runs table.

Reads only — does not mutate jobs/* or runtime/wfo/*. Approve actions live in
run_detail.py; cancel/remove queue actions live here.
"""
from __future__ import annotations
import json
from pathlib import Path

import pandas as pd
import streamlit as st

from ui.wfo.job_state import (
    list_jobs, write_cancel_sentinel, JobRecord,
)


def _format_duration(rec: JobRecord) -> str:
    if rec.completed_at and rec.started_at:
        from datetime import datetime
        dt = (datetime.fromisoformat(rec.completed_at)
              - datetime.fromisoformat(rec.started_at))
        secs = int(dt.total_seconds())
        return f"{secs // 60}m {secs % 60}s"
    return "—"


def _read_run_meta(run_dir: Path) -> dict:
    out = {"universe_size": None, "evaluated": None, "passed": None}
    manifest = run_dir / "manifest.json"
    if manifest.exists():
        try:
            m = json.loads(manifest.read_text())
            out["evaluated"] = m.get("evaluated_groups")
            out["passed"] = m.get("passed_groups")
        except Exception:
            pass
    universe = run_dir / "universe.parquet"
    if universe.exists():
        try:
            out["universe_size"] = len(pd.read_parquet(universe))
        except Exception:
            pass
    return out


def render(jobs_root: Path, runs_root: Path) -> None:
    queued, active, history = list_jobs(jobs_root)

    # --- Active job banner ---
    if active:
        rec = active[0]
        st.subheader(f"⚙ Running: {rec.job_id}")
        p = rec.progress
        if p.total_pairs:
            ratio = (p.completed_pairs / p.total_pairs) if p.total_pairs else 0.0
            st.progress(min(max(ratio, 0.0), 1.0))
        cols = st.columns(4)
        cols[0].metric("Pairs", f"{p.completed_pairs}/{p.total_pairs or '?'}")
        cols[1].metric("Current",
                       f"{p.current_symbol or '—'} · {p.current_timeframe or '—'}")
        cols[2].metric("Elapsed", f"{int(p.elapsed_s)}s")
        cols[3].metric("ETA", f"{int(p.eta_s)}s" if p.eta_s else "—")
        if st.button("Cancel running job", type="secondary"):
            write_cancel_sentinel(jobs_root, rec.job_id)
            st.warning(f"Cancel requested for {rec.job_id}.")
        st.divider()

    # --- Queue ---
    if queued:
        st.subheader(f"Queued ({len(queued)})")
        for rec in queued:
            cols = st.columns([3, 6, 1])
            cols[0].text(rec.job_id)
            cols[1].text(f"queued at {rec.queued_at}")
            if cols[2].button("Remove", key=f"remove_{rec.job_id}"):
                (jobs_root / "queue" / f"{rec.job_id}.json").unlink(missing_ok=True)
                (jobs_root / "configs" / f"{rec.job_id}.yaml").unlink(missing_ok=True)
                st.rerun()
        st.divider()

    # --- Completed runs ---
    st.subheader("Runs")
    runs = sorted([d for d in runs_root.iterdir() if d.is_dir()
                   and d.name not in ("active", "jobs", "presets",
                                       "universe_cache", "latest")],
                  key=lambda p: p.name, reverse=True)
    if not runs:
        st.caption("No runs yet. Create one from **New Run**.")
        return

    history_by_id = {r.run_id: r for r in history}
    rows = []
    for d in runs:
        meta = _read_run_meta(d)
        hrec = history_by_id.get(d.name)
        rows.append({
            "run_id": d.name,
            "status": (hrec.status if hrec else "—"),
            "duration": (_format_duration(hrec) if hrec else "—"),
            "universe": meta["universe_size"] or "—",
            "evaluated": meta["evaluated"] or "—",
            "passed": meta["passed"] or "—",
        })
    df = pd.DataFrame(rows)
    st.dataframe(df, use_container_width=True, hide_index=True)

    chosen = st.selectbox("Open run", options=[""] + [r["run_id"] for r in rows],
                          index=0)
    if chosen:
        st.session_state["wfo_selected_run"] = chosen
        st.session_state["wfo_panel"] = "run_detail"
        st.rerun()
```

- [ ] **Step 9.2: Sanity-import the module**

Run: `python -c "from ui.wfo import runs_list; print('ok')"`
Expected: `ok`.

- [ ] **Step 9.3: Commit**

```bash
git add ui/wfo/runs_list.py
git commit -m "feat(wfo): runs-list panel with active-job banner + queue + completed table"
```

---

## Task 10 — WFO tab: New Run form panel

**Files:**
- Modify: `ui/wfo/forms.py` (append render_form)

- [ ] **Step 10.1: Append `render_form` to `ui/wfo/forms.py`**

Add at the end of `ui/wfo/forms.py`:

```python
# --- Streamlit rendering (no tests; manual smoke) -------------------------

import streamlit as st
import yaml
from pathlib import Path

from ui.wfo.job_state import enqueue


def _load_template() -> dict:
    return yaml.safe_load(Path("config/wfo.yaml").read_text())


def _estimate(p: FormPayload, template: dict, universe_size: int) -> tuple[int, str]:
    """Return (task_count, headline_message). Order of magnitude only."""
    timeframes = len(p.windowing.timeframes)
    # Walk count from windowing — rough bar-day approximation.
    from datetime import date
    try:
        d0 = date.fromisoformat(p.windowing.history_start)
        d1 = date.fromisoformat(p.windowing.history_end)
        total_days = (d1 - d0).days
    except Exception:
        total_days = 0
    is_d = _approx_days(p.windowing.in_sample)
    oos_d = _approx_days(p.windowing.out_of_sample)
    walks = max(0, (total_days - is_d) // max(oos_d, 1))
    combos = 0
    for setup, params in (template.get("grid") or {}).items():
        n = 1
        for v in params.values():
            n *= max(1, len(v))
        combos += n
    pm = template.get("position_management") or {}
    pm_n = 1
    for v in pm.values():
        pm_n *= max(1, len(v))
    combos *= max(1, pm_n)
    tasks = universe_size * timeframes * walks * combos
    msg = (f"Universe: {universe_size} symbols × {timeframes} tf × "
           f"{walks} walks × {combos} combos = {tasks:,} tasks")
    return tasks, msg


def _approx_days(s: str) -> int:
    if s.endswith("mo"):
        return int(s[:-2]) * 30
    if s.endswith("d"):
        return int(s[:-1])
    return 0


def render_form(jobs_root: Path) -> None:
    template = _load_template()
    st.subheader("New WFO run")

    with st.expander("Universe", expanded=True):
        source = st.radio("Source", ["alpaca_scan", "symbols"], horizontal=True)
        if source == "alpaca_scan":
            classes = st.multiselect("Asset classes",
                                     ["us_equity", "crypto"],
                                     default=["us_equity"])
            cols = st.columns(2)
            top_eq = cols[0].number_input(
                "Top-N us_equity (0 = all)", min_value=0, value=100, step=10)
            top_cr = cols[1].number_input(
                "Top-N crypto (0 = all)", min_value=0, value=0, step=10)
            min_dv = st.number_input("Min 20d $-volume",
                                     min_value=0, value=5_000_000, step=500_000)
            allow = st.text_area(
                "Allowlist (force-include, one per line)", height=80)
            block = st.text_area("Blocklist (one per line)", height=80)
            top_n = {"us_equity": (top_eq if top_eq > 0 else None),
                     "crypto":    (top_cr if top_cr > 0 else None)}
            universe = UniverseSpec(
                source="alpaca_scan", classes=classes,
                min_dollar_volume_20d=min_dv, top_n_per_class=top_n,
                allowlist=[s.strip() for s in allow.splitlines() if s.strip()],
                blocklist=[s.strip() for s in block.splitlines() if s.strip()],
                explicit_symbols=[],
            )
        else:
            syms = st.text_area("Symbols (one per line)", height=140)
            universe = UniverseSpec(
                source="symbols", classes=[], min_dollar_volume_20d=0,
                top_n_per_class={}, allowlist=[], blocklist=[],
                explicit_symbols=[s.strip() for s in syms.splitlines() if s.strip()],
            )

    with st.expander("Windowing", expanded=True):
        cols = st.columns(3)
        is_len = cols[0].text_input("IS length", value="6mo")
        oos_len = cols[1].text_input("OOS length", value="1mo")
        step = cols[2].text_input("Step (blank = OOS)", value="")
        cols2 = st.columns(2)
        h_start = cols2[0].text_input("History start (YYYY-MM-DD)",
                                      value="2024-01-01")
        h_end = cols2[1].text_input("History end (YYYY-MM-DD)",
                                    value="2026-04-30")
        timeframes = st.multiselect(
            "Timeframes", ["5Min", "15Min", "30Min", "1Hour"],
            default=["15Min", "30Min"])
        windowing = WindowingSpec(
            in_sample=is_len, out_of_sample=oos_len,
            step=(step or None),
            history_start=h_start, history_end=h_end,
            timeframes=timeframes,
        )

    with st.expander("Gate", expanded=True):
        cols = st.columns(3)
        wfe_min = cols[0].number_input("WFE min", value=0.5, step=0.1, format="%.2f")
        require_pnl = cols[1].checkbox("Require positive OOS P&L", value=True)
        min_trades = cols[2].number_input("Min trades floor (IS)",
                                          min_value=1, value=20)
        gate = GateSpec(wfe_min=float(wfe_min),
                        require_positive_oos_pnl=bool(require_pnl),
                        min_trades=int(min_trades))

    payload = FormPayload(universe=universe, windowing=windowing, gate=gate)

    # Estimate panel — universe_size is unknown until preview/scan; show bound.
    universe_size = len(universe.explicit_symbols) if universe.source == "symbols" \
        else (sum((v or 100) for v in universe.top_n_per_class.values()) or 100)
    _, msg = _estimate(payload, template, universe_size)
    st.caption(msg)

    cols = st.columns([1, 1, 4])
    if cols[0].button("Launch run", type="primary"):
        try:
            validate_form(payload)
        except FormValidationError as e:
            st.error(f"Validation: {e}")
            return
        merged = merge_payload_into_template(payload, template)
        rec = enqueue(jobs_root, payload=_payload_to_dict(payload),
                      wfo_template=merged)
        st.success(f"Queued job {rec.job_id}.")
        st.session_state["wfo_panel"] = "runs_list"
        st.rerun()


def _payload_to_dict(p: FormPayload) -> dict:
    return {
        "universe": {
            "source": p.universe.source,
            "classes": p.universe.classes,
            "min_dollar_volume_20d": p.universe.min_dollar_volume_20d,
            "top_n_per_class": p.universe.top_n_per_class,
            "allowlist": p.universe.allowlist,
            "blocklist": p.universe.blocklist,
            "explicit_symbols": p.universe.explicit_symbols,
        },
        "windowing": p.windowing.__dict__,
        "gate": p.gate.__dict__,
    }
```

- [ ] **Step 10.2: Sanity check imports**

Run: `python -c "from ui.wfo.forms import render_form; print('ok')"`
Expected: `ok`.

- [ ] **Step 10.3: Re-run form-payload tests**

Run: `pytest tests/test_wfo_form_payload.py -v`
Expected: all still pass (we only added a render_form below the data layer).

- [ ] **Step 10.4: Commit**

```bash
git add ui/wfo/forms.py
git commit -m "feat(wfo): New Run form (Streamlit) — universe + windowing + gate"
```

---

## Task 11 — WFO tab: Run Detail panel (charts + approval)

**Files:**
- Create: `ui/wfo/run_detail.py`

- [ ] **Step 11.1: Implement `ui/wfo/run_detail.py`**

```python
"""Run Detail panel: per-symbol comparison table + per-symbol charts +
per-symbol Approve/Reject buttons. Reads runtime/wfo/<run_id>/."""
from __future__ import annotations
import json
from pathlib import Path

import pandas as pd
import streamlit as st
import yaml

from ui.wfo.approval import (
    approve_symbol, GateFailedApproveError, read_active,
)
from ui.wfo.charts import (
    walk_oos_curve, walk_oos_sharpe_bars, is_vs_oos_scatter,
    param_heatmap, pick_heatmap_axes,
    build_equity_fig, build_sharpe_bars_fig, build_is_oos_scatter_fig,
    build_heatmap_fig,
)


def _candidate_for(symbol: str, candidate: dict) -> dict | None:
    return (candidate.get("symbols") or {}).get(symbol)


def _format_params(d: dict) -> str:
    pairs = [f"{k}={v}" for k, v in d.items()]
    return ", ".join(pairs)


def render(run_id: str, runs_root: Path,
           active_path: Path, audit_path: Path) -> None:
    run_dir = runs_root / run_id
    if not run_dir.exists():
        st.error(f"Run {run_id} not found.")
        return

    cols = st.columns([5, 1])
    cols[0].subheader(f"Run {run_id}")
    if cols[1].button("Back to runs"):
        st.session_state["wfo_panel"] = "runs_list"
        st.rerun()

    candidate_path = run_dir / "live_overrides.yaml"
    candidate = (yaml.safe_load(candidate_path.read_text())
                 if candidate_path.exists() else {"symbols": {}})

    manifest_path = run_dir / "manifest.json"
    if manifest_path.exists():
        m = json.loads(manifest_path.read_text())
        st.caption(f"git_sha: {m.get('git_sha')} • "
                   f"evaluated: {m.get('evaluated_groups')} • "
                   f"passed: {m.get('passed_groups')}")

    parquet_path = run_dir / "results.parquet"
    if not parquet_path.exists():
        st.warning("results.parquet not found — run may have crashed.")
        return
    df = pd.read_parquet(parquet_path)

    active = read_active(active_path)
    rows = []
    candidate_symbols = list((candidate.get("symbols") or {}).keys())
    # Also surface gate-failed symbols not in candidate
    all_symbols = sorted(set(df["symbol"].unique()) | set(candidate_symbols))
    for sym in all_symbols:
        cand = _candidate_for(sym, candidate)
        cur = (active.get("symbols") or {}).get(sym)
        rows.append({
            "Symbol": sym,
            "Current": (f"{cur['timeframe']} · {cur['setup']}" if cur else "—"),
            "Candidate": (f"{cand['timeframe']} · {cand['setup']}" if cand
                          else "(failed gate)"),
            "WFE": (cand or {}).get("metadata", {}).get("wfe", "—"),
            "OOS PnL": (cand or {}).get("metadata", {}).get("total_oos_pnl", "—"),
        })
    table = pd.DataFrame(rows)
    st.dataframe(table, use_container_width=True, hide_index=True)

    sym = st.selectbox("Inspect symbol", options=[""] + all_symbols, index=0)
    if not sym:
        return

    cand = _candidate_for(sym, candidate)
    cur = (active.get("symbols") or {}).get(sym)

    cols = st.columns([3, 3, 2])
    cols[0].markdown("**Currently active**")
    cols[0].text(_format_params((cur or {}).get("setup_params", {}))
                 if cur else "(none)")
    cols[1].markdown("**Candidate (this run)**")
    cols[1].text(_format_params((cand or {}).get("setup_params", {}))
                 if cand else "(failed gate)")

    if cand is not None:
        if cols[2].button("Approve", key=f"approve_{sym}"):
            try:
                approve_symbol(active_path=active_path, audit_path=audit_path,
                               symbol=sym, candidate=cand, run_id=run_id)
                st.success(f"Approved {sym}.")
                st.rerun()
            except GateFailedApproveError as e:
                st.error(str(e))
        cols[2].button("Reject", key=f"reject_{sym}")  # non-sticky

    # Charts: pick the (timeframe, setup) for the candidate (or first available).
    tf, setup = (
        (cand["timeframe"], cand["setup"]) if cand
        else (df[df["symbol"] == sym].iloc[0]["timeframe"],
              df[df["symbol"] == sym].iloc[0]["setup"])
    )

    eq = walk_oos_curve(df, sym, tf, setup)
    sb = walk_oos_sharpe_bars(df, sym, tf, setup)
    sc = is_vs_oos_scatter(df, sym, tf, setup)
    axes = pick_heatmap_axes(setup)
    hm = param_heatmap(df, sym, tf, setup, axes=axes)

    st.plotly_chart(build_equity_fig(eq, sym), use_container_width=True)
    st.plotly_chart(build_sharpe_bars_fig(sb, sym), use_container_width=True)
    st.plotly_chart(build_is_oos_scatter_fig(sc, sym), use_container_width=True)
    st.plotly_chart(build_heatmap_fig(hm, sym, axes), use_container_width=True)

    summary_md = run_dir / "summary.md"
    if summary_md.exists():
        with st.expander("Run summary (summary.md)"):
            st.markdown(summary_md.read_text())
```

- [ ] **Step 11.2: Sanity-import**

Run: `python -c "from ui.wfo import run_detail; print('ok')"`
Expected: `ok`.

- [ ] **Step 11.3: Commit**

```bash
git add ui/wfo/run_detail.py
git commit -m "feat(wfo): run-detail panel with comparison table + 4 charts + approve/reject"
```

---

## Task 12 — WFO tab entry + Active Overrides panel + dashboard wiring

**Files:**
- Create: `ui/wfo/active_panel.py`
- Create: `ui/wfo/tab.py`
- Modify: `ui/dashboard.py`

- [ ] **Step 12.1: Implement `ui/wfo/active_panel.py`**

```python
"""Active Overrides panel: read-only list of symbols.<sym> in active YAML
plus a Revert button per row, audit-trail tail, and a 'next-restart' banner."""
from __future__ import annotations
from pathlib import Path

import pandas as pd
import streamlit as st

from ui.wfo.approval import read_active, revert_symbol, read_audit_tail


def render(active_path: Path, audit_path: Path, runs_root: Path) -> None:
    st.info("Changes here take effect on the next trader restart.")

    payload = read_active(active_path)
    syms = payload.get("symbols") or {}

    if not syms:
        st.caption("No active overrides. Live trader uses settings.yaml defaults.")
    else:
        rows = []
        for s, entry in syms.items():
            params = entry.get("setup_params") or {}
            prov = entry.get("_provenance") or {}
            rows.append({
                "Symbol": s,
                "Timeframe": entry.get("timeframe"),
                "Setup": entry.get("setup"),
                "Params": ", ".join(f"{k}={v}" for k, v in params.items()),
                "Run": prov.get("run_id"),
                "Approved at": prov.get("approved_at"),
            })
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

        revert_target = st.selectbox(
            "Revert symbol", options=[""] + list(syms.keys()))
        if revert_target and st.button(f"Revert {revert_target}"):
            revert_symbol(active_path, audit_path, revert_target)
            st.success(f"Reverted {revert_target}.")
            st.rerun()

    with st.expander("Audit trail (last 50)"):
        tail = read_audit_tail(audit_path, n=50)
        if not tail:
            st.caption("No audit entries yet.")
        else:
            st.dataframe(pd.DataFrame(tail),
                         use_container_width=True, hide_index=True)
```

- [ ] **Step 12.2: Implement `ui/wfo/tab.py`**

```python
"""Top-level WFO-tab entry point. Switches between the four panels via
st.session_state['wfo_panel']."""
from __future__ import annotations
from pathlib import Path

import streamlit as st

from ui.wfo import active_panel, forms, run_detail, runs_list, supervisor


_RUNS_ROOT = Path("runtime/wfo")
_JOBS_ROOT = _RUNS_ROOT / "jobs"
_ACTIVE_DIR = _RUNS_ROOT / "active"
_ACTIVE_FILE = _ACTIVE_DIR / "live_overrides.yaml"
_AUDIT_FILE = _ACTIVE_DIR / "audit.jsonl"


def render() -> None:
    # Boot the supervisor thread (idempotent).
    supervisor.get_or_start_supervisor(_JOBS_ROOT)

    panel = st.session_state.get("wfo_panel", "runs_list")
    cols = st.columns(4)
    if cols[0].button("Runs", use_container_width=True,
                      type=("primary" if panel in ("runs_list", "run_detail")
                            else "secondary")):
        st.session_state["wfo_panel"] = "runs_list"
        st.rerun()
    if cols[1].button("New Run", use_container_width=True,
                      type=("primary" if panel == "new_run" else "secondary")):
        st.session_state["wfo_panel"] = "new_run"
        st.rerun()
    if cols[2].button("Active overrides", use_container_width=True,
                      type=("primary" if panel == "active" else "secondary")):
        st.session_state["wfo_panel"] = "active"
        st.rerun()
    cols[3].empty()
    st.divider()

    panel = st.session_state.get("wfo_panel", "runs_list")
    _RUNS_ROOT.mkdir(parents=True, exist_ok=True)
    _ACTIVE_DIR.mkdir(parents=True, exist_ok=True)

    if panel == "runs_list":
        runs_list.render(_JOBS_ROOT, _RUNS_ROOT)
    elif panel == "run_detail":
        run_id = st.session_state.get("wfo_selected_run")
        if not run_id:
            st.session_state["wfo_panel"] = "runs_list"
            st.rerun()
        else:
            run_detail.render(run_id, _RUNS_ROOT, _ACTIVE_FILE, _AUDIT_FILE)
    elif panel == "new_run":
        forms.render_form(_JOBS_ROOT)
    elif panel == "active":
        active_panel.render(_ACTIVE_FILE, _AUDIT_FILE, _RUNS_ROOT)
```

- [ ] **Step 12.3: Wire into `ui/dashboard.py`**

Modify `ui/dashboard.py`. Replace the line:

```python
overview_tab, logs_tab = st.tabs(["Overview", "Logs"])
```

with:

```python
from ui.wfo import tab as wfo_tab

overview_tab, logs_tab, wfo_tab_panel = st.tabs(["Overview", "Logs", "WFO"])
```

And append at the end of the file:

```python
with wfo_tab_panel:
    wfo_tab.render()
```

- [ ] **Step 12.4: Sanity-import + run full suite**

Run: `python -c "from ui.wfo import tab; from ui import dashboard; print('ok')"`
Expected: `ok`.

Run: `TRADING_ENV=test pytest -x`
Expected: full suite passes.

- [ ] **Step 12.5: Commit**

```bash
git add ui/wfo/active_panel.py ui/wfo/tab.py ui/dashboard.py
git commit -m "feat(wfo): WFO tab — active-overrides panel + tab switcher + dashboard wiring"
```

---

## Task 13 — docker-compose: dashboard env_file + remove TRADING_ENV=test

**Files:**
- Modify: `docker-compose.yml`

- [ ] **Step 13.1: Update `docker-compose.yml`**

In the `dashboard:` service block, replace:

```yaml
    environment:
      - TRADING_ENV=test  # prevents lock.file import-time sys.exit
      - STATE_FILE_PATH=/app/runtime/trading_state.json
      - PYTHONPATH=/app
```

with:

```yaml
    env_file: ./config/.env
    environment:
      - STATE_FILE_PATH=/app/runtime/trading_state.json
      - PYTHONPATH=/app
```

(Drops `TRADING_ENV=test` — dashboard process never imports `main.py`, so the variable was a no-op. Adds `env_file` so the dashboard has Alpaca creds for universe scan + WFO subprocess.)

- [ ] **Step 13.2: Manual smoke (dev box)**

Run: `docker compose up --build -d dashboard`
Then in a browser: `http://127.0.0.1:8501` — confirm the **WFO** tab loads, the three sub-panels (`Runs`, `New Run`, `Active overrides`) render, and the **Overview** + **Logs** tabs still work.

If the dashboard container fails to import `ui.wfo.*`, check the dashboard logs:
Run: `docker compose logs dashboard | tail -40`

- [ ] **Step 13.3: Commit**

```bash
git add docker-compose.yml
git commit -m "feat(docker): dashboard env_file=config/.env + drop no-op TRADING_ENV=test

Wires Alpaca creds into the dashboard container so the WFO tab can scan
the universe, fetch bars, and spawn the run_wfo subprocess."
```

- [ ] **Step 13.4: Final regression run**

Run: `TRADING_ENV=test pytest -x`
Expected: full suite (including the existing 178 tests + the new ones from Tasks 1, 4, 5, 6, 7, 8) passes.

---

## Notes for the executor

- **TDD discipline**: Tasks 1, 4, 5, 6, 7, 8 are TDD-strict (test-first). Tasks 9, 10, 11, 12 are UI rendering with no unit tests — verify by sanity-import + manual smoke. Task 13 is config-only with manual smoke.
- **Filesystem invariants**: `runtime/wfo/active/`, `runtime/wfo/jobs/`, and `runtime/wfo/jobs/{queue,active,history,configs}/` are created on demand by helpers — never expect them to pre-exist.
- **Subprocess testing**: the supervisor tests (Task 8) inject a `_StubProc` factory; do not spawn real subprocesses in the test suite.
- **Docker rebuild**: every `Modified` change to a `.py` file under `ui/` must rebuild the dashboard container (`docker compose build dashboard`) before manual smoke. Do this once at the end of Task 12 — there's no need to rebuild between earlier tasks because the manual smoke only happens in Task 13.
- **Plotly dependency**: already pinned in `requirements.txt`, no install step needed.
- **Resume semantics**: a job whose subprocess crashed mid-run leaves a partial `runtime/wfo/<run_id>/results.parquet`. Re-launching with the same form values from the **New Run** form generates a fresh `job_id` and therefore a fresh `run_id` — true resume requires extending the form to accept a `--run-id` to override (out of scope; flagged for a v2 follow-up).
