"""WFO aggregation, gate, and live-overrides emission."""
from __future__ import annotations
import json
import logging
import math
import os
from dataclasses import dataclass
from pathlib import Path

import pandas as pd
import yaml

logger = logging.getLogger(__name__)


@dataclass
class GateConfig:
    wfe_min: float = 0.5
    require_positive_oos_pnl: bool = True


def aggregate_results(results: pd.DataFrame, gate: GateConfig) -> pd.DataFrame:
    """For each (symbol, timeframe, setup): pick per-walk IS-best, compute WFE,
    apply gate. Returns one row per group with aggregate stats + pass/fail."""
    ok = results[results["status"] == "ok"]
    if ok.empty:
        return pd.DataFrame(columns=[
            "symbol", "timeframe", "setup", "walks",
            "sum_is_sharpe", "sum_oos_sharpe", "wfe",
            "total_oos_pnl", "mean_oos_sharpe", "passed",
            "winning_fingerprint_last_walk",
        ])

    # Per-walk IS-best within each (symbol, timeframe, setup)
    keys = ["symbol", "timeframe", "setup"]
    winners = (
        ok.sort_values("is_sharpe", ascending=False)
          .groupby(keys + ["walk_idx"], as_index=False)
          .head(1)
    )

    rows = []
    for (sym, tf, stp), g in winners.groupby(keys):
        g = g.sort_values("walk_idx")
        sum_is = float(g["is_sharpe"].sum())
        sum_oos = float(g["oos_sharpe"].sum())
        total_oos_pnl = float(g["oos_pnl"].sum())
        wfe = sum_oos / sum_is if sum_is > 0 else math.nan
        passed = (
            (not math.isnan(wfe))
            and (wfe >= gate.wfe_min)
            and ((total_oos_pnl > 0) if gate.require_positive_oos_pnl else True)
        )
        last_walk_winner = g.iloc[-1]["fingerprint"]
        rows.append({
            "symbol": sym, "timeframe": tf, "setup": stp,
            "walks": int(len(g)),
            "sum_is_sharpe": sum_is, "sum_oos_sharpe": sum_oos,
            "wfe": wfe, "total_oos_pnl": total_oos_pnl,
            "mean_oos_sharpe": float(g["oos_sharpe"].mean()),
            "passed": passed,
            "winning_fingerprint_last_walk": last_walk_winner,
        })
    df_out = pd.DataFrame(rows, dtype=object)
    # Restore proper dtypes for numeric columns, but keep 'passed' as object (Python bool)
    for col in ["walks", "sum_is_sharpe", "sum_oos_sharpe", "wfe", "total_oos_pnl", "mean_oos_sharpe"]:
        if col in df_out.columns:
            if col == "wfe":
                df_out[col] = df_out[col].astype(float)
            else:
                df_out[col] = pd.to_numeric(df_out[col])
    return df_out
