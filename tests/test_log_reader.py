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
