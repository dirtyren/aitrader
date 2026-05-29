"""Gap-and-Go scanner: universe + daily candidate selection.

Owns the daily lifecycle from 03:35 (baseline refresh) through 08:30
(scanner cut). Splits cleanly into pure logic and broker-data calls so
unit tests can drive the cut and ranking without network.

Public surface:
    - ScanResult: frozen dataclass returned by run_cut for each kept symbol
    - GapScanner: stateful object holding the universe, baselines, and
      running pre-market snapshot per symbol

Baseline cache JSON (one entry per symbol):
    {
      "AAPL": {
        "atr_14d": 3.42,
        "avg_premarket_volume_20d": 850000,
        "avg_daily_volume_20d": 52400000,
        "computed_at": "2026-05-23T03:35:00Z"
      }
    }
"""
from __future__ import annotations

import csv
import json
import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ScanResult:
    symbol: str
    gap_pct: float          # signed, in percent
    gap_atr_mult: float     # signed, |gap_dollars|/atr — used for ranking
    rvol: float             # premarket_volume / avg_premarket_volume_20d
    premarket_high: float
    premarket_low: float
    premarket_vwap: float
    last_price: float
    atr_14d: float
    side: str               # "long" only in v1
    cut_ts: datetime


@dataclass
class _CandidateState:
    """Running pre-market state per symbol, updated by candidate_status()."""
    last_price: float = 0.0
    prev_close: float = 0.0
    premarket_high: float = float("-inf")
    premarket_low: float = float("inf")
    premarket_volume: float = 0.0
    _pv_sum: float = 0.0    # sum of price*volume (for vwap)
    _v_sum: float = 0.0     # sum of volume
    last_update_ts: datetime | None = None

    @property
    def premarket_vwap(self) -> float:
        if self._v_sum <= 0:
            return self.last_price
        return self._pv_sum / self._v_sum


@dataclass
class _Baseline:
    atr_14d: float
    avg_premarket_volume_20d: float
    avg_daily_volume_20d: float
    computed_at: datetime


# ---------------------------------------------------------------------------
# Filters config
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ScannerFilters:
    min_price: float = 5.0
    min_avg_daily_volume: float = 1_000_000.0
    min_rvol: float = 5.0
    min_gap_pct: float = 4.0
    min_gap_atr_mult: float = 1.5


@dataclass(frozen=True)
class ScannerRanking:
    candidate_multiplier: float = 1.5      # top_N = max_concurrent * this


# ---------------------------------------------------------------------------
# GapScanner
# ---------------------------------------------------------------------------


class GapScanner:
    """Owns the universe, the baseline cache, and the per-symbol pre-market state."""

    def __init__(
        self,
        universe: list[str],
        baselines: dict[str, _Baseline],
        baselines_max_age_days: int = 7,
        filters: ScannerFilters = ScannerFilters(),
        ranking: ScannerRanking = ScannerRanking(),
        max_concurrent_positions: int = 4,
    ) -> None:
        if not universe:
            raise ValueError("GapScanner universe is empty")
        self.universe = list(universe)
        self.baselines = dict(baselines)
        self.baselines_max_age_days = baselines_max_age_days
        self.filters = filters
        self.ranking = ranking
        self.max_concurrent_positions = max_concurrent_positions
        self._state: dict[str, _CandidateState] = {
            s: _CandidateState() for s in self.universe
        }

    # ── Universe / baselines I/O ────────────────────────────────────────

    @staticmethod
    def load_universe(path: str | Path) -> list[str]:
        """Read a CSV of symbols (one per line, optional 'symbol' header)."""
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"Universe file not found: {p}")
        symbols: list[str] = []
        with p.open() as f:
            reader = csv.reader(f)
            for i, row in enumerate(reader):
                if not row:
                    continue
                cell = row[0].strip()
                if not cell:
                    continue
                if i == 0 and cell.lower() == "symbol":
                    continue
                symbols.append(cell.upper())
        return symbols

    @staticmethod
    def load_baselines(path: str | Path) -> dict[str, _Baseline]:
        """Load the baselines JSON. Missing file yields {} (caller can refresh)."""
        p = Path(path)
        if not p.exists():
            return {}
        try:
            raw = json.loads(p.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("BASELINES_LOAD_FAILED path=%s error=%s", p, exc)
            return {}
        out: dict[str, _Baseline] = {}
        for sym, entry in (raw or {}).items():
            try:
                out[sym] = _Baseline(
                    atr_14d=float(entry["atr_14d"]),
                    avg_premarket_volume_20d=float(entry["avg_premarket_volume_20d"]),
                    avg_daily_volume_20d=float(entry["avg_daily_volume_20d"]),
                    computed_at=datetime.fromisoformat(
                        entry["computed_at"].replace("Z", "+00:00")
                    ),
                )
            except (KeyError, ValueError) as exc:
                logger.warning("BASELINE_SKIP symbol=%s error=%s", sym, exc)
        return out

    @staticmethod
    def save_baselines(baselines: dict[str, _Baseline], path: str | Path) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            sym: {
                "atr_14d": b.atr_14d,
                "avg_premarket_volume_20d": b.avg_premarket_volume_20d,
                "avg_daily_volume_20d": b.avg_daily_volume_20d,
                "computed_at": b.computed_at.astimezone(timezone.utc)
                .isoformat()
                .replace("+00:00", "Z"),
            }
            for sym, b in baselines.items()
        }
        # Atomic-ish write: tmp + rename.
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True))
        tmp.replace(p)

    def baselines_age_days(self, now: datetime) -> float | None:
        """Age of the OLDEST baseline, in days. None if any symbol is missing."""
        if not self.baselines:
            return None
        oldest = min(b.computed_at for b in self.baselines.values())
        return (now - oldest).total_seconds() / 86400.0

    def baselines_are_stale(self, now: datetime) -> bool:
        """True when refresh is recommended."""
        age = self.baselines_age_days(now)
        if age is None:
            return True
        return age > self.baselines_max_age_days

    def baselines_too_old_to_trade(self, now: datetime) -> bool:
        """True when baselines are 2x past the recommended max — hard fail-safe."""
        age = self.baselines_age_days(now)
        if age is None:
            return True
        return age > self.baselines_max_age_days * 2

    # ── Snapshot tracking (04:00 → 08:30) ──────────────────────────────

    def candidate_status(self, snapshots: dict[str, dict],
                         now: datetime) -> None:
        """Apply one bulk-snapshot poll to the running pre-market state.

        Each value in ``snapshots`` is the per-symbol dict returned by
        Alpaca's snapshot endpoint, conveying at minimum:
            {
              "latestTrade": {"p": <price>, "s": <last-trade-size>},
              "minuteBar":   {"h": <high>, "l": <low>, "v": <vol>, "vw": <vwap>},
              "prevDailyBar":{"c": <prev_close>},
            }

        Missing fields fall back to whatever is available; the snapshot is
        ignored (logged at debug) when neither price nor volume is parseable.
        """
        for sym, snap in snapshots.items():
            if sym not in self._state:
                continue
            state = self._state[sym]
            last_trade = (snap.get("latestTrade") or {})
            minute_bar = (snap.get("minuteBar") or {})
            prev_daily = (snap.get("prevDailyBar") or {})

            try:
                last = float(last_trade.get("p") or minute_bar.get("c") or 0)
            except (TypeError, ValueError):
                last = 0.0
            try:
                prev_close = float(prev_daily.get("c") or 0)
            except (TypeError, ValueError):
                prev_close = 0.0
            try:
                m_high = float(minute_bar.get("h") or 0)
                m_low = float(minute_bar.get("l") or 0)
                m_vol = float(minute_bar.get("v") or 0)
                m_vwap = float(minute_bar.get("vw") or 0)
            except (TypeError, ValueError):
                m_high = m_low = m_vol = m_vwap = 0.0

            if last > 0:
                state.last_price = last
            if prev_close > 0:
                state.prev_close = prev_close
            if m_high > 0:
                state.premarket_high = max(state.premarket_high, m_high)
            if m_low > 0:
                state.premarket_low = min(state.premarket_low, m_low)
            if m_vol > 0:
                state.premarket_volume += m_vol
                if m_vwap > 0:
                    state.premarket_vwap_last = m_vwap
                    state._pv_sum += m_vwap * m_vol
                    state._v_sum += m_vol
            state.last_update_ts = now

    def get_state(self, symbol: str) -> _CandidateState | None:
        return self._state.get(symbol)

    # ── Cut at 08:30 ────────────────────────────────────────────────────

    def run_cut(self, now: datetime) -> list[ScanResult]:
        """Apply filters + ranking and return the day's watchlist.

        Symbols missing baselines, prev_close, or any pre-market data are
        silently dropped (no signal is preferable to a wrong signal).
        """
        if self.baselines_too_old_to_trade(now):
            logger.error(
                "SCANNER_BASELINES_TOO_STALE age_days=%.1f max=%d — refusing to cut",
                self.baselines_age_days(now) or float("inf"),
                self.baselines_max_age_days * 2,
            )
            return []

        f = self.filters
        candidates: list[ScanResult] = []
        for sym in self.universe:
            state = self._state.get(sym)
            base = self.baselines.get(sym)
            if state is None or base is None:
                continue
            if state.prev_close <= 0 or state.last_price <= 0:
                continue
            if state.premarket_high == float("-inf") or state.premarket_low == float("inf"):
                continue
            if base.atr_14d <= 0:
                continue
            if base.avg_premarket_volume_20d <= 0:
                continue

            gap_dollars = state.last_price - state.prev_close
            gap_pct = gap_dollars / state.prev_close * 100.0
            gap_atr_mult = abs(gap_dollars) / base.atr_14d
            rvol = state.premarket_volume / base.avg_premarket_volume_20d

            # Filter cascade — every condition must pass.
            if state.last_price < f.min_price:
                continue
            if base.avg_daily_volume_20d < f.min_avg_daily_volume:
                continue
            if rvol < f.min_rvol:
                continue
            if abs(gap_pct) < f.min_gap_pct:
                continue
            if gap_atr_mult < f.min_gap_atr_mult:
                continue
            # Long-only in v1.
            if gap_pct <= 0:
                continue

            candidates.append(ScanResult(
                symbol=sym,
                gap_pct=gap_pct,
                gap_atr_mult=gap_atr_mult,
                rvol=rvol,
                premarket_high=state.premarket_high,
                premarket_low=state.premarket_low,
                premarket_vwap=state.premarket_vwap,
                last_price=state.last_price,
                atr_14d=base.atr_14d,
                side="long",
                cut_ts=now,
            ))

        # Rank by gap_atr_mult * rvol descending.
        candidates.sort(key=lambda c: c.gap_atr_mult * c.rvol, reverse=True)

        top_n = max(1, math.ceil(self.max_concurrent_positions
                                 * self.ranking.candidate_multiplier))
        return candidates[:top_n]

    # ── Reset (new day) ────────────────────────────────────────────────

    def reset_for_new_day(self) -> None:
        """Clear per-day pre-market state. Called after EOD."""
        self._state = {s: _CandidateState() for s in self.universe}
