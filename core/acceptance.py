from __future__ import annotations
from core.bar import Bar


def _last_n_closes(bars: list[Bar], n: int) -> list[float] | None:
    if len(bars) < n:
        return None
    return [b.close for b in bars[-n:]]


def accepted_above(bars: list[Bar], level: float, n: int,
                   min_distance_atr: float, atr: float) -> bool:
    closes = _last_n_closes(bars, n)
    if closes is None:
        return False
    if not all(c > level for c in closes):
        return False
    farthest = max(closes) - level
    return farthest >= min_distance_atr * atr


def accepted_below(bars: list[Bar], level: float, n: int,
                   min_distance_atr: float, atr: float) -> bool:
    closes = _last_n_closes(bars, n)
    if closes is None:
        return False
    if not all(c < level for c in closes):
        return False
    farthest = level - min(closes)
    return farthest >= min_distance_atr * atr
