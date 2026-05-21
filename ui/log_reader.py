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
