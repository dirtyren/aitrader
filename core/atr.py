from __future__ import annotations
from core.bar import Bar


def _true_range(prev_close: float | None, b: Bar) -> float:
    if prev_close is None:
        return b.high - b.low
    return max(
        b.high - b.low,
        abs(b.high - prev_close),
        abs(b.low - prev_close),
    )


def atr(bars: list[Bar], window: int) -> float:
    """Wilder-style ATR; falls back to mean of available TRs when bars < window."""
    if not bars:
        return 0.0
    trs: list[float] = []
    prev_c: float | None = None
    for b in bars:
        trs.append(_true_range(prev_c, b))
        prev_c = b.close
    if len(trs) < window:
        return sum(trs) / len(trs)
    # Wilder smoothing
    smoothed = sum(trs[:window]) / window
    for tr in trs[window:]:
        smoothed = (smoothed * (window - 1) + tr) / window
    return smoothed
