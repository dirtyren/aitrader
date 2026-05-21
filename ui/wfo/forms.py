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


# --- Streamlit rendering (no tests; manual smoke) -------------------------

import streamlit as st
import yaml
from pathlib import Path

from ui.wfo.job_state import enqueue


def _load_template() -> dict:
    return yaml.safe_load(Path("config/wfo.yaml").read_text())


def _estimate(p: FormPayload, template: dict, universe_size: int) -> tuple[int, str]:
    """Return (task_count, headline_message). Order of magnitude only."""
    timeframes = len(p.windowing.timeframes)
    # Walk count from windowing — rough bar-day approximation.
    from datetime import date
    try:
        d0 = date.fromisoformat(p.windowing.history_start)
        d1 = date.fromisoformat(p.windowing.history_end)
        total_days = (d1 - d0).days
    except Exception:
        total_days = 0
    is_d = _approx_days(p.windowing.in_sample)
    oos_d = _approx_days(p.windowing.out_of_sample)
    walks = max(0, (total_days - is_d) // max(oos_d, 1))
    combos = 0
    for setup, params in (template.get("grid") or {}).items():
        n = 1
        for v in params.values():
            n *= max(1, len(v))
        combos += n
    pm = template.get("position_management") or {}
    pm_n = 1
    for v in pm.values():
        pm_n *= max(1, len(v))
    combos *= max(1, pm_n)
    tasks = universe_size * timeframes * walks * combos
    msg = (f"Universe: {universe_size} symbols × {timeframes} tf × "
           f"{walks} walks × {combos} combos = {tasks:,} tasks")
    return tasks, msg


def _approx_days(s: str) -> int:
    if s.endswith("mo"):
        return int(s[:-2]) * 30
    if s.endswith("d"):
        return int(s[:-1])
    return 0


def render_form(jobs_root: Path) -> None:
    template = _load_template()
    st.subheader("New WFO run")

    with st.expander("Universe", expanded=True):
        source = st.radio("Source", ["alpaca_scan", "symbols"], horizontal=True)
        if source == "alpaca_scan":
            classes = st.multiselect("Asset classes",
                                     ["us_equity", "crypto"],
                                     default=["us_equity"])
            cols = st.columns(2)
            top_eq = cols[0].number_input(
                "Top-N us_equity (0 = all)", min_value=0, value=100, step=10)
            top_cr = cols[1].number_input(
                "Top-N crypto (0 = all)", min_value=0, value=0, step=10)
            min_dv = st.number_input("Min 20d $-volume",
                                     min_value=0, value=5_000_000, step=500_000)
            allow = st.text_area(
                "Allowlist (force-include, one per line)", height=80)
            block = st.text_area("Blocklist (one per line)", height=80)
            top_n = {"us_equity": (top_eq if top_eq > 0 else None),
                     "crypto":    (top_cr if top_cr > 0 else None)}
            universe = UniverseSpec(
                source="alpaca_scan", classes=classes,
                min_dollar_volume_20d=min_dv, top_n_per_class=top_n,
                allowlist=[s.strip() for s in allow.splitlines() if s.strip()],
                blocklist=[s.strip() for s in block.splitlines() if s.strip()],
                explicit_symbols=[],
            )
        else:
            syms = st.text_area("Symbols (one per line)", height=140)
            universe = UniverseSpec(
                source="symbols", classes=[], min_dollar_volume_20d=0,
                top_n_per_class={}, allowlist=[], blocklist=[],
                explicit_symbols=[s.strip() for s in syms.splitlines() if s.strip()],
            )

    with st.expander("Windowing", expanded=True):
        cols = st.columns(3)
        is_len = cols[0].text_input("IS length", value="6mo")
        oos_len = cols[1].text_input("OOS length", value="1mo")
        step = cols[2].text_input("Step (blank = OOS)", value="")
        cols2 = st.columns(2)
        h_start = cols2[0].text_input("History start (YYYY-MM-DD)",
                                      value="2024-01-01")
        h_end = cols2[1].text_input("History end (YYYY-MM-DD)",
                                    value="2026-04-30")
        timeframes = st.multiselect(
            "Timeframes", ["5Min", "15Min", "30Min", "1Hour"],
            default=["15Min", "30Min"])
        windowing = WindowingSpec(
            in_sample=is_len, out_of_sample=oos_len,
            step=(step or None),
            history_start=h_start, history_end=h_end,
            timeframes=timeframes,
        )

    with st.expander("Gate", expanded=True):
        cols = st.columns(3)
        wfe_min = cols[0].number_input("WFE min", value=0.5, step=0.1, format="%.2f")
        require_pnl = cols[1].checkbox("Require positive OOS P&L", value=True)
        min_trades = cols[2].number_input("Min trades floor (IS)",
                                          min_value=1, value=20)
        gate = GateSpec(wfe_min=float(wfe_min),
                        require_positive_oos_pnl=bool(require_pnl),
                        min_trades=int(min_trades))

    payload = FormPayload(universe=universe, windowing=windowing, gate=gate)

    # Estimate panel — universe_size is unknown until preview/scan; show bound.
    universe_size = len(universe.explicit_symbols) if universe.source == "symbols" \
        else (sum((v or 100) for v in universe.top_n_per_class.values()) or 100)
    _, msg = _estimate(payload, template, universe_size)
    st.caption(msg)

    cols = st.columns([1, 1, 4])
    if cols[0].button("Launch run", type="primary"):
        try:
            validate_form(payload)
        except FormValidationError as e:
            st.error(f"Validation: {e}")
            return
        merged = merge_payload_into_template(payload, template)
        rec = enqueue(jobs_root, payload=_payload_to_dict(payload),
                      wfo_template=merged)
        st.success(f"Queued job {rec.job_id}.")
        st.session_state["wfo_panel"] = "runs_list"
        st.rerun()


def _payload_to_dict(p: FormPayload) -> dict:
    return {
        "universe": {
            "source": p.universe.source,
            "classes": p.universe.classes,
            "min_dollar_volume_20d": p.universe.min_dollar_volume_20d,
            "top_n_per_class": p.universe.top_n_per_class,
            "allowlist": p.universe.allowlist,
            "blocklist": p.universe.blocklist,
            "explicit_symbols": p.universe.explicit_symbols,
        },
        "windowing": p.windowing.__dict__,
        "gate": p.gate.__dict__,
    }
