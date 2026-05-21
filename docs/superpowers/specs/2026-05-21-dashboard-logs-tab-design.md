# Dashboard Logs Tab — Design

**Date:** 2026-05-21
**Status:** Draft, awaiting user sign-off

## Goal

Add a Logs tab to the existing Streamlit dashboard so the operator can read the trader's structured logs without shelling into the container. Levels are visually distinguished, the user can filter by level, the tail can be paused, and a manual refresh button forces an immediate re-read.

## Non-goals

- Cross-process true streaming (WebSocket / push). Polling at the dashboard's existing 5s cadence is sufficient.
- Editing or rotating logs from the UI.
- Aggregating logs from multiple files / containers.
- Search across the full file history (only the tail is in scope).

## User stories

1. While the engine is running, the operator opens the dashboard, clicks the **Logs** tab, and sees the most recent ~500 lines of `logs/vwap_wave.log` with each line tagged by level.
2. The operator unchecks DEBUG to suppress noise; only INFO/WARNING/ERROR remain visible.
3. To investigate a specific event, the operator pauses the live tail (toggle off), reads at leisure, then re-enables.
4. After applying a fix, the operator clicks **Refresh now** to immediately pull the latest tail without waiting for the 5s tick.

## Architecture

### Page layout

`ui/dashboard.py` wraps its current content in `st.tabs(["Overview", "Logs"])`. The Overview tab keeps today's behavior unchanged; the Logs tab delegates to `ui.logs_panel.render(log_path)`.

A single `st_autorefresh(interval=5_000, …)` remains at module scope, driving both tabs. The Logs tab gates its file read on `st.session_state["logs_live"]`; when paused, it reuses the last snapshot stored in `st.session_state["logs_snapshot"]` so the rendered content stays static.

### Modules

| File | Role |
|---|---|
| `ui/dashboard.py` | Top-level Streamlit script. Reads config, instantiates the two tabs. |
| `ui/logs_panel.py` | New. Renders the Logs tab: controls row + log viewer. Pure rendering — no file I/O. |
| `ui/log_reader.py` | New. Pure tail/parse logic with no Streamlit dependency. Unit-testable. |
| `tests/test_log_reader.py` | New. Tests for tail correctness, line parsing, traceback continuation. |

### `log_reader` contract

```python
@dataclass(frozen=True, slots=True)
class ParsedLine:
    timestamp: str        # raw timestamp string from the log line
    level: str            # one of: DEBUG, INFO, WARNING, ERROR, CRITICAL, UNKNOWN
    logger: str           # logger name; "" for unparseable rows
    message: str          # the message body; for continuation rows, appended to the prior line

def tail(path: str | Path, n: int) -> list[ParsedLine]:
    """Return up to the last n logical log entries from `path`.

    A 'logical entry' is one parseable header line plus any subsequent
    non-header (continuation / traceback) lines, merged into its message.
    Returns an empty list if the file does not exist.
    """
```

Implementation notes:
- Reads backwards in 64 KiB blocks until ≥ N parseable header lines are accumulated; avoids loading whole files.
- Header pattern: `^<timestamp> | <LEVEL>\s+| <logger>\s+| <message>$` matched with a single regex anchored on the leading timestamp shape (`\d{4}-\d{2}-\d{2}\s\d{2}:\d{2}:\d{2}[.,]\d{3}`).
- Lines that don't match the header pattern are appended to the previous logical entry's `message` (joined with `\n`). If no prior entry exists, they're emitted as `level="UNKNOWN", logger=""`.

### `logs_panel.render`

```python
def render(log_path: str | Path) -> None:
    """Render the Logs tab. Reads st.session_state for live/refresh controls."""
```

Layout, top to bottom:

1. **Controls row** — four columns:
   - Multiselect of levels (`["DEBUG", "INFO", "WARNING", "ERROR"]`), default `["INFO", "WARNING", "ERROR"]`. State key: `logs_levels`.
   - Number input for tail size, default 500, min 100, max 5000, step 100. State key: `logs_tail`.
   - Toggle "Live (auto-refresh)", default on. State key: `logs_live`.
   - Button "Refresh now". On click, sets `logs_force_refresh = True` for the current rerun.

2. **Status caption** — small line: "Showing last N entries · live · last read HH:MM:SS" or " · paused".

3. **Log viewer** — single `st.markdown(unsafe_allow_html=True)` block containing one `<div>` per logical entry, newest first. Each row:
   ```html
   <div class="log-row level-ERROR">
     <span class="log-badge">ERROR</span>
     <span class="log-ts">2026-05-20 19:40:11,228</span>
     <span class="log-logger">regime_trader</span>
     <span class="log-msg">BREAKEVEN_REPLACE_FAILED ...</span>
   </div>
   ```
   - CSS injected once via `st.markdown` at the top of `render()`.
   - Color tokens — ERROR `#ff4b4b`, WARNING `#ffb84d`, INFO `#4b9eff`, DEBUG `#888`, UNKNOWN `#aaa`.
   - Multi-line messages preserve newlines via `white-space: pre-wrap` on `.log-msg`.
   - HTML-escape the timestamp/logger/message fields before injection.

### Refresh / live-toggle wiring

The page-level `st_autorefresh` ticks every 5s regardless. On each rerun, `logs_panel.render`:

```
if logs_force_refresh OR logs_live:
    snapshot = log_reader.tail(path, n)
    st.session_state["logs_snapshot"] = (snapshot, now)
    st.session_state["logs_force_refresh"] = False
else:
    snapshot, _ = st.session_state.get("logs_snapshot", ([], None))

filtered = [r for r in snapshot if r.level in logs_levels]
render filtered
```

Effect: pausing freezes the displayed content; **Refresh now** forces one read and updates the snapshot; the Overview tab is unaffected because it has its own read path.

### Configuration

The log file path comes from `cfg["logging"]["log_file"]`. The dashboard already loads `config/settings.yaml`; if not, it gains a small loader (or a default path constant `logs/vwap_wave.log` consistent with `setup_logging`).

## Error handling

- Missing log file → empty list, status caption: "No log file at `<path>` yet."
- Unreadable / IO error during tail → caught, displayed as a single warning row in the viewer; does not crash the page.
- Parse failure on individual lines → fall through to the continuation/UNKNOWN path described above.

## Testing

`tests/test_log_reader.py`:
- `test_tail_returns_empty_for_missing_file`
- `test_tail_returns_all_lines_when_file_smaller_than_n`
- `test_tail_returns_last_n_when_file_larger`
- `test_parse_extracts_timestamp_level_logger_message`
- `test_traceback_lines_attached_to_prior_entry`
- `test_unparseable_leading_lines_yield_unknown_level`
- `test_backward_block_read_handles_block_boundary` — line straddles 64 KiB read boundary, must still be returned correctly.

No new tests for the Streamlit panel itself; existing dashboard tests cover JSON contract and we maintain that pattern.

## Out of scope / future

- Real-time WebSocket streaming.
- Search-by-symbol-or-substring (the design leaves room for it as another widget in the controls row).
- Multi-file log aggregation.
- Persistent UI preferences across sessions.

## Open risks

- Backward-block tail must handle the trailing partial line correctly when the file's last block doesn't end on a newline. The unit test enumerated above covers this.
- Very large files (> 100 MiB) would still be cheap to tail since we read backwards, but parsing a multi-line stack trace that starts before the read window means its head is silently truncated; we accept this — the operator can increase tail size.
