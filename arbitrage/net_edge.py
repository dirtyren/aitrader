"""Net edge (after costs) and its distribution statistics."""
import numpy as np
import pandas as pd


def net_bps(gross_quote: pd.Series, mid_quote: pd.Series, cost_bps: float) -> pd.Series:
    """Gross spread (quote units) as bps of mid, minus round-trip cost in bps."""
    return gross_quote / mid_quote * 1e4 - cost_bps


def net_edge_stats(net: pd.Series, grid_ms: int) -> dict:
    """Summary stats of net edge; persistence = runs of net > 0 scaled by grid_ms."""
    if len(net) == 0:
        return {
            "median_bps": 0.0,
            "mean_bps": 0.0,
            "pct_positive": 0.0,
            "persistence_median_ms": 0.0,
            "persistence_max_ms": 0.0,
        }
    pos = (net > 0).to_numpy()
    # run lengths of consecutive True values
    edges = np.diff(np.concatenate(([0], pos.astype(int), [0])))
    runs = np.flatnonzero(edges == -1) - np.flatnonzero(edges == 1)
    return {
        "median_bps": float(net.median()),
        "mean_bps": float(net.mean()),
        "pct_positive": float(pos.mean() * 100),
        "persistence_median_ms": float(np.median(runs) * grid_ms) if len(runs) else 0.0,
        "persistence_max_ms": float(runs.max() * grid_ms) if len(runs) else 0.0,
    }
