from datetime import datetime, timezone, timedelta

import pytest

from ui.components.period_selector import resolve_preset, PRESETS


NOW = datetime(2026, 5, 28, 15, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize("preset, days", [
    ("1D", 1), ("1W", 7), ("15D", 15), ("1M", 30), ("6M", 180), ("1Y", 365),
])
def test_resolve_preset_rolling_windows(preset, days):
    start, end = resolve_preset(preset, now=NOW)
    assert end == NOW
    assert start == NOW - timedelta(days=days)


def test_presets_list_matches_spec():
    assert PRESETS == ["1D", "1W", "15D", "1M", "6M", "1Y"]


def test_resolve_preset_unknown_raises():
    with pytest.raises(ValueError):
        resolve_preset("3Y", now=NOW)
