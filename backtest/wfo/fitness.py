"""IS-fitness scoring for parameter combos."""
from __future__ import annotations
import math


def score(metrics: dict, min_trades: int) -> float | None:
    """Return Sharpe iff trades >= min_trades and Sharpe is finite; else None."""
    if metrics.get("trades", 0) < min_trades:
        return None
    sharpe = metrics.get("sharpe", float("nan"))
    if not isinstance(sharpe, (int, float)) or math.isnan(sharpe) or math.isinf(sharpe):
        return None
    return float(sharpe)
