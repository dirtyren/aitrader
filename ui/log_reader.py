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
