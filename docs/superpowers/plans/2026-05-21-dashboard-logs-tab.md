# Dashboard Logs Tab Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a Logs tab to the existing Streamlit dashboard that tails `logs/vwap_wave.log`, color-tags each entry by level, supports a level filter, tail size, live/paused toggle, and a manual refresh button.

**Architecture:** Two-tab layout via `st.tabs(["Overview", "Logs"])` in `ui/dashboard.py`. The Logs tab delegates to `ui.logs_panel.render(log_path)`. A pure, Streamlit-free `ui.log_reader.tail(path, n) -> list[ParsedLine]` reads the file backwards in 64 KiB blocks, parses lines on the existing `ts | LEVEL | logger | msg` format, and merges traceback / continuation lines into the previous logical entry. Page-level `st_autorefresh(5_000)` drives both tabs; the Logs tab gates re-reads on a `logs_live` session-state flag and on a force-refresh flag set by the Refresh button.

**Tech Stack:** Python 3.11, Streamlit, streamlit-autorefresh, pytest. No new dependencies.

---

## File Structure

| File | Status | Responsibility |
|---|---|---|
| `ui/log_reader.py` | new | `ParsedLine` dataclass + `tail(path, n)` (no Streamlit) |
| `ui/logs_panel.py` | new | `render(log_path)` — controls + viewer markdown |
| `ui/dashboard.py` | modify | wrap existing UI in tab 1, mount logs_panel in tab 2 |
| `tests/test_log_reader.py` | new | unit tests for tail/parse/continuation/boundary |

No tests for `logs_panel` or `dashboard.py` (matches existing project pattern: only the JSON state contract has UI-adjacent tests via `tests/test_dashboard_state.py`).

---

## Task 1: `ParsedLine` dataclass + tail/parse stub

**Files:**
- Create: `ui/log_reader.py`
- Test: `tests/test_log_reader.py`

- [ ] **Step 1: Write the failing tests for the dataclass and empty-file behavior**

Create `tests/test_log_reader.py`:

```python
from pathlib import Path

from ui.log_reader import ParsedLine, tail


def test_parsed_line_fields():
    p = ParsedLine(timestamp="2026-05-20 19:40:11,228",
                   level="ERROR", logger="regime_trader",
                   message="hello")
    assert p.level == "ERROR"
    assert p.message == "hello"


def test_tail_missing_file_returns_empty(tmp_path: Path):
    assert tail(tmp_path / "does-not-exist.log", n=10) == []


def test_tail_empty_file_returns_empty(tmp_path: Path):
    f = tmp_path / "empty.log"
    f.write_text("")
    assert tail(f, n=10) == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_log_reader.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'ui.log_reader'`.

- [ ] **Step 3: Implement minimal `ParsedLine` and stub `tail`**

Create `ui/log_reader.py`:

```python
"""Tail/parse for the regime_trader log file.

Format produced by `ui/logging_setup.py`:
    "%(asctime)s | %(levelname)-8s | %(name)-30s | %(message)s"

Multi-line messages (e.g. tracebacks) emit one header line followed by
continuation lines that don't match the header pattern. This module
merges those into the prior logical entry.

No Streamlit dependency — pure I/O and parsing, unit-testable.
"""
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class ParsedLine:
    timestamp: str
    level: str
    logger: str
    message: str


def tail(path: str | Path, n: int) -> list[ParsedLine]:
    p = Path(path)
    if not p.exists():
        return []
    return []  # filled in next task
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_log_reader.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add ui/log_reader.py tests/test_log_reader.py
git commit -m "feat(ui): scaffold ParsedLine + tail() stub for log reader"
```

---

## Task 2: Header-line parser

**Files:**
- Modify: `ui/log_reader.py`
- Test: `tests/test_log_reader.py`

- [ ] **Step 1: Write failing tests for header parsing and continuation merging**

Append to `tests/test_log_reader.py`:

```python
from ui.log_reader import _parse_header, _merge_lines


def test_parse_header_matches_standard_line():
    line = "2026-05-20 19:40:11,228 | ERROR    | regime_trader                  | BREAKEVEN_REPLACE_FAILED symbol=NVDA"
    parsed = _parse_header(line)
    assert parsed is not None
    assert parsed.timestamp == "2026-05-20 19:40:11,228"
    assert parsed.level == "ERROR"
    assert parsed.logger == "regime_trader"
    assert parsed.message == "BREAKEVEN_REPLACE_FAILED symbol=NVDA"


def test_parse_header_returns_none_for_continuation_line():
    assert _parse_header("Traceback (most recent call last):") is None
    assert _parse_header("    File \"/app/x.py\", line 1, in foo") is None


def test_merge_lines_attaches_continuations_to_prior_entry():
    raw = [
        "2026-05-20 19:40:11,228 | ERROR    | regime_trader                  | BREAKEVEN_REPLACE_FAILED symbol=NVDA",
        "Traceback (most recent call last):",
        "    File \"/app/x.py\", line 1, in foo",
        "RuntimeError: bad",
        "2026-05-20 19:40:11,233 | INFO     | regime_trader                  | NEXT_LINE",
    ]
    out = _merge_lines(raw)
    assert len(out) == 2
    assert out[0].level == "ERROR"
    assert "Traceback" in out[0].message
    assert "RuntimeError: bad" in out[0].message
    assert out[1].level == "INFO"
    assert out[1].message == "NEXT_LINE"


def test_merge_lines_orphan_continuation_emits_unknown():
    raw = [
        "this does not match anything",
        "neither does this",
        "2026-05-20 19:40:11,228 | INFO     | x                              | hi",
    ]
    out = _merge_lines(raw)
    assert len(out) == 2
    assert out[0].level == "UNKNOWN"
    assert "this does not match anything\nneither does this" == out[0].message
    assert out[1].level == "INFO"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_log_reader.py -v`
Expected: FAIL with `ImportError: cannot import name '_parse_header'`.

- [ ] **Step 3: Implement `_parse_header` and `_merge_lines`**

Replace the body of `ui/log_reader.py` (keeping the docstring and `ParsedLine`) with:

```python
"""Tail/parse for the regime_trader log file.

Format produced by `ui/logging_setup.py`:
    "%(asctime)s | %(levelname)-8s | %(name)-30s | %(message)s"

Multi-line messages (e.g. tracebacks) emit one header line followed by
continuation lines that don't match the header pattern. This module
merges those into the prior logical entry.

No Streamlit dependency — pure I/O and parsing, unit-testable.
"""
from __future__ import annotations
import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class ParsedLine:
    timestamp: str
    level: str
    logger: str
    message: str


_HEADER_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2}\s\d{2}:\d{2}:\d{2}[.,]\d{3})"
    r"\s\|\s(?P<level>[A-Z]+)\s*"
    r"\|\s(?P<logger>\S+)\s*"
    r"\|\s(?P<msg>.*)$"
)


def _parse_header(line: str) -> ParsedLine | None:
    m = _HEADER_RE.match(line)
    if not m:
        return None
    return ParsedLine(
        timestamp=m.group("ts"),
        level=m.group("level"),
        logger=m.group("logger"),
        message=m.group("msg"),
    )


def _merge_lines(raw_lines: list[str]) -> list[ParsedLine]:
    """Group raw lines into logical entries, attaching continuations.

    Lines preceding the first header become a single UNKNOWN entry.
    """
    out: list[ParsedLine] = []
    pending_orphans: list[str] = []
    for line in raw_lines:
        parsed = _parse_header(line)
        if parsed is None:
            if out:
                last = out[-1]
                merged = ParsedLine(
                    timestamp=last.timestamp, level=last.level,
                    logger=last.logger,
                    message=f"{last.message}\n{line}" if last.message else line,
                )
                out[-1] = merged
            else:
                pending_orphans.append(line)
            continue
        if pending_orphans:
            out.append(ParsedLine(
                timestamp="", level="UNKNOWN", logger="",
                message="\n".join(pending_orphans),
            ))
            pending_orphans = []
        out.append(parsed)
    if pending_orphans:
        out.append(ParsedLine(
            timestamp="", level="UNKNOWN", logger="",
            message="\n".join(pending_orphans),
        ))
    return out


def tail(path: str | Path, n: int) -> list[ParsedLine]:
    p = Path(path)
    if not p.exists():
        return []
    return []  # implemented in next task
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_log_reader.py -v`
Expected: 7 passed.

- [ ] **Step 5: Commit**

```bash
git add ui/log_reader.py tests/test_log_reader.py
git commit -m "feat(ui): parse log header lines and merge tracebacks"
```

---

## Task 3: Backwards-block tail implementation

**Files:**
- Modify: `ui/log_reader.py`
- Test: `tests/test_log_reader.py`

- [ ] **Step 1: Write failing tests for `tail()` against real files**

Append to `tests/test_log_reader.py`:

```python
def _write_log(path: Path, n_entries: int) -> None:
    lines = []
    for i in range(n_entries):
        lines.append(
            f"2026-05-20 19:40:{i % 60:02d},000 | INFO     | regime_trader                  | line {i}"
        )
    path.write_text("\n".join(lines) + "\n")


def test_tail_returns_all_when_file_smaller_than_n(tmp_path: Path):
    f = tmp_path / "small.log"
    _write_log(f, 5)
    out = tail(f, n=20)
    assert len(out) == 5
    assert out[0].message == "line 0"
    assert out[-1].message == "line 4"


def test_tail_returns_last_n_when_file_larger(tmp_path: Path):
    f = tmp_path / "big.log"
    _write_log(f, 1000)
    out = tail(f, n=50)
    assert len(out) == 50
    assert out[0].message == "line 950"
    assert out[-1].message == "line 999"


def test_tail_handles_block_boundary(tmp_path: Path):
    """Verify a header line straddling the 64 KiB read boundary is recovered."""
    f = tmp_path / "wide.log"
    # Build > 64 KiB so multiple backwards reads happen.
    padding_msg = "x" * 200
    lines = [
        f"2026-05-20 19:40:{i % 60:02d},000 | INFO     | regime_trader                  | {padding_msg} {i}"
        for i in range(500)
    ]
    f.write_text("\n".join(lines) + "\n")
    out = tail(f, n=10)
    assert len(out) == 10
    assert out[-1].message.endswith("499")
    assert out[0].message.endswith("490")


def test_tail_includes_traceback_with_header(tmp_path: Path):
    f = tmp_path / "trace.log"
    f.write_text(
        "2026-05-20 19:40:11,228 | ERROR    | regime_trader                  | BOOM\n"
        "Traceback (most recent call last):\n"
        "    File \"x.py\", line 1\n"
        "RuntimeError: bad\n"
        "2026-05-20 19:40:11,233 | INFO     | regime_trader                  | next\n"
    )
    out = tail(f, n=10)
    assert len(out) == 2
    assert out[0].level == "ERROR"
    assert "Traceback" in out[0].message
    assert "RuntimeError: bad" in out[0].message
    assert out[1].level == "INFO"


def test_tail_file_with_no_trailing_newline(tmp_path: Path):
    f = tmp_path / "noeol.log"
    f.write_text(
        "2026-05-20 19:40:11,228 | INFO     | regime_trader                  | one\n"
        "2026-05-20 19:40:11,229 | INFO     | regime_trader                  | two"
    )
    out = tail(f, n=10)
    assert len(out) == 2
    assert out[-1].message == "two"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_log_reader.py -v`
Expected: 5 new tests fail (`assert len(out) == 5` etc.) because `tail()` still returns `[]`.

- [ ] **Step 3: Implement backwards-block read**

Replace the `tail()` function in `ui/log_reader.py`:

```python
_BLOCK = 64 * 1024


def tail(path: str | Path, n: int) -> list[ParsedLine]:
    """Return up to the last n logical log entries.

    Reads the file backwards in 64 KiB blocks, accumulates raw lines,
    and stops once at least n header lines are buffered (so traceback
    continuations attached to the n-th oldest header are also included).
    """
    p = Path(path)
    if not p.exists():
        return []
    size = p.stat().st_size
    if size == 0:
        return []

    parts: list[bytes] = []
    pos = size
    header_count = 0
    with p.open("rb") as f:
        while pos > 0 and header_count <= n:
            read_size = min(_BLOCK, pos)
            pos -= read_size
            f.seek(pos)
            block = f.read(read_size)
            parts.append(block)
            joined = b"".join(reversed(parts))
            text = joined.decode("utf-8", errors="replace")
            # Count header lines accumulated so far. Cheap: only run
            # the regex against line starts.
            header_count = sum(
                1 for line in text.splitlines()
                if _HEADER_RE.match(line)
            )

    text = b"".join(reversed(parts)).decode("utf-8", errors="replace")
    raw_lines = text.splitlines()
    # If we didn't read from the very start, the first line may be a
    # partial cut — drop it to avoid emitting a corrupt header.
    if pos > 0 and raw_lines:
        raw_lines = raw_lines[1:]
    merged = _merge_lines(raw_lines)
    return merged[-n:] if n < len(merged) else merged
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_log_reader.py -v`
Expected: 12 passed.

- [ ] **Step 5: Commit**

```bash
git add ui/log_reader.py tests/test_log_reader.py
git commit -m "feat(ui): backwards-block tail() for log reader"
```

---

## Task 4: `logs_panel.render` — controls + viewer

**Files:**
- Create: `ui/logs_panel.py`

This task has no automated tests (Streamlit panel, matches existing dashboard pattern). Manual verification at the end.

- [ ] **Step 1: Create the panel module**

Create `ui/logs_panel.py`:

```python
"""Streamlit Logs tab.

Renders a level-filtered tail of the trader log file, using
`ui.log_reader.tail` for the heavy lifting. Live-toggle and refresh
button are wired through st.session_state; the page-level
st_autorefresh in dashboard.py drives the periodic re-read.
"""
from __future__ import annotations
import html
import time
from datetime import datetime
from pathlib import Path

import streamlit as st

from ui.log_reader import ParsedLine, tail

_LEVEL_COLORS = {
    "ERROR": "#ff4b4b",
    "WARNING": "#ffb84d",
    "INFO": "#4b9eff",
    "DEBUG": "#888888",
    "CRITICAL": "#ff4b4b",
    "UNKNOWN": "#aaaaaa",
}

_CSS = """
<style>
.log-row { font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
           font-size: 12px; padding: 2px 0; line-height: 1.4; }
.log-badge { display: inline-block; min-width: 60px; padding: 0 6px;
             margin-right: 6px; border-radius: 3px; color: white;
             font-weight: 600; text-align: center; }
.log-ts { color: #aaa; margin-right: 6px; }
.log-logger { color: #888; margin-right: 6px; }
.log-msg { white-space: pre-wrap; }
</style>
"""


def render(log_path: str | Path) -> None:
    st.markdown(_CSS, unsafe_allow_html=True)

    st.session_state.setdefault("logs_levels", ["INFO", "WARNING", "ERROR"])
    st.session_state.setdefault("logs_tail", 500)
    st.session_state.setdefault("logs_live", True)
    st.session_state.setdefault("logs_force_refresh", False)
    st.session_state.setdefault("logs_snapshot", ([], None))

    c1, c2, c3, c4 = st.columns([3, 2, 1, 1])
    levels = c1.multiselect(
        "Levels",
        options=["DEBUG", "INFO", "WARNING", "ERROR"],
        default=st.session_state["logs_levels"],
        key="logs_levels",
    )
    n = c2.number_input(
        "Tail size", min_value=100, max_value=5000,
        value=st.session_state["logs_tail"], step=100,
        key="logs_tail",
    )
    live = c3.toggle("Live", value=st.session_state["logs_live"], key="logs_live")
    if c4.button("Refresh now"):
        st.session_state["logs_force_refresh"] = True

    should_read = st.session_state["logs_force_refresh"] or live
    if should_read:
        snapshot = tail(log_path, n=int(n))
        st.session_state["logs_snapshot"] = (snapshot, datetime.now())
        st.session_state["logs_force_refresh"] = False

    snapshot, last_read = st.session_state["logs_snapshot"]
    status = "live" if live else "paused"
    last_read_str = last_read.strftime("%H:%M:%S") if last_read else "—"
    st.caption(f"Showing last {len(snapshot)} entries · {status} · last read {last_read_str}")

    if not snapshot:
        st.info(f"No log entries yet at `{log_path}`.")
        return

    filtered = [r for r in snapshot if r.level in levels or
                (r.level == "UNKNOWN" and "INFO" in levels)]
    rows_html = "\n".join(_render_row(r) for r in reversed(filtered))
    st.markdown(rows_html, unsafe_allow_html=True)


def _render_row(r: ParsedLine) -> str:
    color = _LEVEL_COLORS.get(r.level, _LEVEL_COLORS["UNKNOWN"])
    return (
        f'<div class="log-row">'
        f'<span class="log-badge" style="background:{color}">{html.escape(r.level)}</span>'
        f'<span class="log-ts">{html.escape(r.timestamp)}</span>'
        f'<span class="log-logger">{html.escape(r.logger)}</span>'
        f'<span class="log-msg">{html.escape(r.message)}</span>'
        f'</div>'
    )
```

- [ ] **Step 2: Smoke-import the new module**

Run: `.venv/bin/python -c "from ui.logs_panel import render; print('ok')"`
Expected: `ok`.

- [ ] **Step 3: Commit**

```bash
git add ui/logs_panel.py
git commit -m "feat(ui): logs_panel render — controls and viewer"
```

---

## Task 5: Wire two tabs into `ui/dashboard.py`

**Files:**
- Modify: `ui/dashboard.py`

- [ ] **Step 1: Update dashboard.py to add tabs and a log path**

Replace the body of `ui/dashboard.py` with:

```python
"""VWAP Wave dashboard.

Two tabs:
  - Overview: equity, day P&L, circuit level, per-symbol regime/VWAP
    table, and recent filter rejects (sourced from runtime/trading_state.json).
  - Logs:  tail of logs/vwap_wave.log with level filter and live toggle.
"""
import json
from pathlib import Path

import pandas as pd
import streamlit as st
import yaml
from streamlit_autorefresh import st_autorefresh

from ui.logs_panel import render as render_logs

STATE_FILE = Path("runtime/trading_state.json")
DEFAULT_LOG_FILE = Path("logs/vwap_wave.log")


def _read_state() -> dict | None:
    if not STATE_FILE.exists():
        return None
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return None


def _resolve_log_file() -> Path:
    cfg_path = Path("config/settings.yaml")
    if not cfg_path.exists():
        return DEFAULT_LOG_FILE
    try:
        cfg = yaml.safe_load(cfg_path.read_text()) or {}
    except Exception:
        return DEFAULT_LOG_FILE
    return Path(cfg.get("logging", {}).get("log_file") or DEFAULT_LOG_FILE)


st.set_page_config(page_title="VWAP Wave", layout="wide")
st_autorefresh(interval=5_000, key="vwap_wave_refresh")
st.title("VWAP Wave Protocol")

overview_tab, logs_tab = st.tabs(["Overview", "Logs"])

with overview_tab:
    state = _read_state()
    if not state or "equity" not in state:
        st.warning("No state file yet. Start the engine via `python main.py`.")
    else:
        col1, col2, col3, col4 = st.columns(4)
        col1.metric("Equity", f"${state['equity']:,.2f}")
        col2.metric("Day P&L", f"${state['day_pnl']:,.2f}")
        col3.metric("Circuit Level", state["circuit_level"])
        col4.metric("As of", state["timestamp"])

        st.subheader("Symbols")
        rows = []
        for s in state.get("symbols", []):
            pos = s.get("open_position")
            rows.append({
                "Symbol": s["symbol"],
                "Regime": s.get("regime"),
                "VWAP": s.get("vwap"),
                "Upper σ": s.get("upper"),
                "Lower σ": s.get("lower"),
                "Position": (f"{pos['side']} {pos['qty']} @ {pos['entry']}" if pos else "—"),
                "Stop": pos["stop"] if pos else "",
                "Target": pos["target"] if pos else "",
            })
        df = pd.DataFrame(rows)
        st.dataframe(df, use_container_width=True, hide_index=True)

        st.subheader("Recent filter rejects")
        rejects = state.get("recent_filter_rejects", [])
        if rejects:
            st.dataframe(pd.DataFrame(rejects), use_container_width=True, hide_index=True)
        else:
            st.caption("No rejects in the recent window.")

with logs_tab:
    render_logs(_resolve_log_file())
```

- [ ] **Step 2: Smoke-import the dashboard module**

Run: `.venv/bin/python -c "import ui.dashboard" 2>&1 | head -5`
Expected: import succeeds, or only Streamlit's "missing ScriptRunContext" warning. No traceback.

- [ ] **Step 3: Manual verification**

Run: `.venv/bin/streamlit run ui/dashboard.py --server.headless true --server.port 8765 &`
Then visit http://localhost:8765 in a browser:
- Both tabs render
- "Logs" tab shows tail of `logs/vwap_wave.log` (or "No log entries yet" if file missing)
- DEBUG checkbox toggle hides/shows DEBUG rows
- Live toggle pauses/resumes auto-update
- Refresh button forces an immediate read

Stop the server: `kill %1`.

If you can't run the browser, this manual step can be skipped and verified by the user.

- [ ] **Step 4: Commit**

```bash
git add ui/dashboard.py
git commit -m "feat(ui): wire Logs tab into dashboard"
```

---

## Task 6: Run full test suite

- [ ] **Step 1: Run all tests**

Run: `.venv/bin/python -m pytest --no-header`
Expected: all previous tests still pass; new `tests/test_log_reader.py` adds ~12 passing tests. Total ≥ 204.

- [ ] **Step 2: If anything regressed, stop and investigate before claiming done**

No commit step — this is a verification gate.

---

## Self-Review Notes

- **Spec coverage:** Task 1–3 cover `log_reader.tail` and parsing requirements (header regex, traceback merging, backwards-block read, missing/empty file, block boundary, no trailing newline). Task 4 covers controls row, viewer markdown, color mapping, HTML escaping, live/pause/refresh wiring. Task 5 covers tab layout, log-path resolution from `cfg["logging"]["log_file"]`, fallback to `logs/vwap_wave.log`. Task 6 is regression check.
- **Placeholder scan:** No "TBD" / "TODO" / "similar to" left.
- **Type consistency:** `ParsedLine` fields (`timestamp/level/logger/message`) used identically across reader, panel, and tests. `tail(path, n)` signature consistent across tasks.
- **Spec gap noted:** the spec doesn't enumerate UNKNOWN-level rendering color; the plan adds `UNKNOWN` to `_LEVEL_COLORS` (`#aaaaaa`) and includes UNKNOWN rows when INFO is in the level filter (sensible default — operators want to see unparsed lines but not as "errors").
