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
            header_count = sum(
                1 for line in text.splitlines()
                if _HEADER_RE.match(line)
            )

    text = b"".join(reversed(parts)).decode("utf-8", errors="replace")
    raw_lines = text.splitlines()
    if pos > 0 and raw_lines:
        raw_lines = raw_lines[1:]
    merged = _merge_lines(raw_lines)
    return merged[-n:] if n < len(merged) else merged
