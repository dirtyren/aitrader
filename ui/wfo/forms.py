"""WFO New Run form: dataclasses, validation, payload→template merge.

UI rendering (Streamlit widgets) is added later in this file under render_form().
"""
from __future__ import annotations
import copy
import re
from dataclasses import dataclass, field

_DURATION_RE = re.compile(r"^\d+(d|mo)$")


class FormValidationError(ValueError):
    pass


@dataclass
class UniverseSpec:
    source: str  # "alpaca_scan" | "symbols"
    classes: list[str] = field(default_factory=list)
    min_dollar_volume_20d: float = 5_000_000
    top_n_per_class: dict[str, int | None] = field(default_factory=dict)
    allowlist: list[str] = field(default_factory=list)
    blocklist: list[str] = field(default_factory=list)
    explicit_symbols: list[str] = field(default_factory=list)


@dataclass
class WindowingSpec:
    in_sample: str = "6mo"
    out_of_sample: str = "1mo"
    step: str | None = None
    history_start: str = "2024-01-01"
    history_end: str = "2026-04-30"
    timeframes: list[str] = field(default_factory=list)


@dataclass
class GateSpec:
    wfe_min: float = 0.5
    require_positive_oos_pnl: bool = True
    min_trades: int = 20


@dataclass
class FormPayload:
    universe: UniverseSpec
    windowing: WindowingSpec
    gate: GateSpec


def validate_form(p: FormPayload) -> None:
    if p.universe.source == "alpaca_scan":
        if not p.universe.classes:
            raise FormValidationError(
                "Universe: at least one of classes is required for alpaca_scan")
    elif p.universe.source == "symbols":
        if not p.universe.explicit_symbols:
            raise FormValidationError(
                "Universe: at least one of symbols is required")
    else:
        raise FormValidationError(
            f"Universe: unknown source {p.universe.source!r}")

    if not p.windowing.timeframes:
        raise FormValidationError("Windowing: at least one of timeframes required")
    for k in ("in_sample", "out_of_sample"):
        v = getattr(p.windowing, k)
        if not _DURATION_RE.match(v):
            raise FormValidationError(
                f"Windowing: {k} must match <int>(d|mo), got {v!r}")
    if p.windowing.step is not None and not _DURATION_RE.match(p.windowing.step):
        raise FormValidationError("Windowing: step must be blank or <int>(d|mo)")

    if not (0.0 < p.gate.wfe_min <= 5.0):
        raise FormValidationError("Gate: wfe_min must be in (0, 5]")
    if p.gate.min_trades < 1:
        raise FormValidationError("Gate: min_trades must be >= 1")


def merge_payload_into_template(p: FormPayload, template: dict) -> dict:
    out = copy.deepcopy(template)

    if p.universe.source == "alpaca_scan":
        scan = out.setdefault("universe", {}).setdefault("alpaca_scan", {})
        scan["classes"] = list(p.universe.classes)
        scan["min_dollar_volume_20d"] = p.universe.min_dollar_volume_20d
        scan["top_n_per_class"] = dict(p.universe.top_n_per_class)
        if p.universe.allowlist:
            scan["allowlist"] = list(p.universe.allowlist)
        if p.universe.blocklist:
            scan["blocklist"] = list(p.universe.blocklist)
        out["universe"]["source"] = "alpaca_scan"
    else:
        out["universe"] = {"source": "symbols",
                           "symbols": list(p.universe.explicit_symbols)}

    win = out.setdefault("windowing", {})
    win["in_sample"] = p.windowing.in_sample
    win["out_of_sample"] = p.windowing.out_of_sample
    win["step"] = p.windowing.step

    out.setdefault("history", {})
    out["history"]["start"] = p.windowing.history_start
    out["history"]["end"] = p.windowing.history_end

    out["timeframes"] = list(p.windowing.timeframes)

    out.setdefault("gate", {})
    out["gate"]["wfe_min"] = p.gate.wfe_min
    out["gate"]["require_positive_oos_pnl"] = p.gate.require_positive_oos_pnl

    out.setdefault("fitness", {})
    out["fitness"]["min_trades"] = p.gate.min_trades

    return out
