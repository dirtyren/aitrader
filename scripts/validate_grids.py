"""Fail-fast precondition: every expected grid file exists and parses.

Run at container startup before the trader process boots. Catches missing or
malformed grid JSON early, not in the middle of a sweep.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
GRIDS_DIR = REPO_ROOT / "scripts" / "grids"

EXPECTED: dict[str, list[str]] = {
    "vwap_wave": [
        "vwap_wave.price_discovery.json",
        "vwap_wave.fade_extreme.json",
        "vwap_wave.return_to_value.json",
        "vwap_wave.vwap_bounce.json",
    ],
    "orb": ["orb.json"],
    "rsi": ["rsi.json"],
    "vwap_bands": ["vwap_bands.json"],
    "ib": ["ib.json"],
}


def validate(strategy: str | None = None) -> int:
    targets = EXPECTED if strategy is None else {strategy: EXPECTED.get(strategy, [])}
    if strategy and strategy not in EXPECTED:
        print(f"validate_grids: unknown strategy {strategy!r}", file=sys.stderr)
        return 2

    errors: list[str] = []
    for name, files in targets.items():
        for fname in files:
            path = GRIDS_DIR / fname
            if not path.exists():
                errors.append(f"missing: {path}")
                continue
            try:
                data = json.loads(path.read_text())
            except json.JSONDecodeError as e:
                errors.append(f"invalid JSON in {path}: {e}")
                continue
            if not isinstance(data, dict) or not data:
                errors.append(f"{path}: expected non-empty object")
                continue
            for key, values in data.items():
                if not isinstance(values, list) or not values:
                    errors.append(f"{path}: '{key}' must be a non-empty list")

    if errors:
        for e in errors:
            print(f"validate_grids: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else None
    sys.exit(validate(arg))
