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
