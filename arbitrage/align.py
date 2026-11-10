import numpy as np
import pandas as pd


def align_pair(
    quotes_by_venue: dict[str, pd.DataFrame], grid_ms: int, staleness_ms: int
) -> pd.DataFrame:
    """As-of align venues onto a common grid.

    Each grid point takes every venue's most recent quote at or before it
    (last-observation-carried-forward). Rows where any venue has no quote yet,
    or whose latest quote is older than ``staleness_ms``, are dropped.
    """
    venues = list(quotes_by_venue)
    min_ts = min(int(q["ts_utc_ms"].min()) for q in quotes_by_venue.values())
    max_ts = max(int(q["ts_utc_ms"].max()) for q in quotes_by_venue.values())
    grid = pd.DataFrame(
        {"ts_utc_ms": np.arange(min_ts, max_ts + 1, grid_ms, dtype="int64")}
    )

    out = grid.copy()
    keep = pd.Series(True, index=out.index)
    for venue in venues:
        q = quotes_by_venue[venue][["ts_utc_ms", "bid", "ask"]].copy()
        q["ts_utc_ms"] = q["ts_utc_ms"].astype("int64")
        q["_venue_ts"] = q["ts_utc_ms"]
        q = q.sort_values("ts_utc_ms", kind="stable").rename(
            columns={"bid": f"bid_{venue}", "ask": f"ask_{venue}"}
        )
        merged = pd.merge_asof(grid, q, on="ts_utc_ms", direction="backward")
        age = merged["ts_utc_ms"] - merged["_venue_ts"]  # NaN if no prior quote
        keep &= (age <= staleness_ms) & age.notna()
        out[f"bid_{venue}"] = merged[f"bid_{venue}"]
        out[f"ask_{venue}"] = merged[f"ask_{venue}"]

    return out[keep].set_index("ts_utc_ms")


def clock_skew_stats(quotes_by_venue: dict[str, pd.DataFrame]) -> dict[str, float]:
    """Per-venue median timestamp minus the first venue's median timestamp."""
    ref = float(next(iter(quotes_by_venue.values()))["ts_utc_ms"].median())
    return {
        venue: float(q["ts_utc_ms"].median()) - ref
        for venue, q in quotes_by_venue.items()
    }
