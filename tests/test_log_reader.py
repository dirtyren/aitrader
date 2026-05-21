from pathlib import Path

from ui.log_reader import ParsedLine, _merge_lines, _parse_header, tail


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
