"""Chart data-prep helpers — pure DataFrame transforms over results.parquet."""
from __future__ import annotations
import math

import pandas as pd
import pytest

from ui.wfo.charts import (
    walk_oos_curve, walk_oos_sharpe_bars, is_vs_oos_scatter,
    param_heatmap, pick_heatmap_axes,
)


def _row(symbol="AAPL", timeframe="15Min", setup="price_discovery",
         walk_idx=0, fingerprint="fp1", combo_values_json='{}',
         is_sharpe=1.0, is_trades=25, is_pnl=100.0,
         oos_sharpe=0.8, oos_trades=10, oos_pnl=50.0,
         oos_max_dd=-20.0, oos_avg_R=0.5, status="ok", error=None,
         asset_class="us_equity") -> dict:
    return locals().copy()


def _df(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(rows)


def test_walk_oos_curve_picks_is_winners_per_walk():
    """For each walk, the IS-best combo wins; OOS pnl from that row is plotted."""
    rows = [
        _row(walk_idx=0, fingerprint="a", is_sharpe=1.0, oos_pnl=100.0),
        _row(walk_idx=0, fingerprint="b", is_sharpe=2.0, oos_pnl=20.0),
        _row(walk_idx=1, fingerprint="a", is_sharpe=1.5, oos_pnl=10.0),
        _row(walk_idx=1, fingerprint="b", is_sharpe=0.5, oos_pnl=80.0),
    ]
    df = walk_oos_curve(_df(rows), "AAPL", "15Min", "price_discovery")
    assert df["walk_idx"].tolist() == [0, 1]
    assert df["oos_pnl"].tolist() == [20.0, 10.0]
    assert df["cumulative_oos_pnl"].tolist() == [20.0, 30.0]


def test_walk_oos_sharpe_bars():
    rows = [
        _row(walk_idx=0, fingerprint="a", is_sharpe=2.0, oos_sharpe=0.7),
        _row(walk_idx=0, fingerprint="b", is_sharpe=1.0, oos_sharpe=2.0),
        _row(walk_idx=1, fingerprint="a", is_sharpe=1.0, oos_sharpe=-0.5),
    ]
    df = walk_oos_sharpe_bars(_df(rows), "AAPL", "15Min", "price_discovery")
    assert df["walk_idx"].tolist() == [0, 1]
    assert df["oos_sharpe"].tolist() == [0.7, -0.5]


def test_is_vs_oos_scatter_one_point_per_walk():
    rows = [
        _row(walk_idx=0, fingerprint="a", is_sharpe=2.0, oos_sharpe=0.7),
        _row(walk_idx=0, fingerprint="b", is_sharpe=1.0, oos_sharpe=2.5),
        _row(walk_idx=1, fingerprint="a", is_sharpe=1.5, oos_sharpe=-0.3),
    ]
    df = is_vs_oos_scatter(_df(rows), "AAPL", "15Min", "price_discovery")
    assert df["walk_idx"].tolist() == [0, 1]
    assert df["is_sharpe"].tolist() == [2.0, 1.5]
    assert df["oos_sharpe"].tolist() == [0.7, -0.3]


def test_param_heatmap_means_oos_sharpe_per_param_pair():
    rows = [
        _row(walk_idx=0, fingerprint="a", oos_sharpe=1.0,
             combo_values_json='{"atr_mult_stop": 1.0, "target_R": 1.5}'),
        _row(walk_idx=1, fingerprint="a", oos_sharpe=0.0,
             combo_values_json='{"atr_mult_stop": 1.0, "target_R": 1.5}'),
        _row(walk_idx=0, fingerprint="b", oos_sharpe=2.0,
             combo_values_json='{"atr_mult_stop": 1.0, "target_R": 2.0}'),
    ]
    df = param_heatmap(_df(rows), "AAPL", "15Min", "price_discovery",
                       axes=("atr_mult_stop", "target_R"))
    cell = df[(df["atr_mult_stop"] == 1.0) & (df["target_R"] == 1.5)]
    assert cell["mean_oos_sharpe"].iloc[0] == pytest.approx(0.5)
    cell2 = df[(df["atr_mult_stop"] == 1.0) & (df["target_R"] == 2.0)]
    assert cell2["mean_oos_sharpe"].iloc[0] == pytest.approx(2.0)


def test_pick_heatmap_axes_per_setup():
    assert pick_heatmap_axes("price_discovery") == ("atr_mult_stop", "target_R")
    assert pick_heatmap_axes("vwap_bounce") == ("atr_mult_stop", "target_R")
    assert pick_heatmap_axes("fade_extreme") == ("atr_mult_stop", "max_hold_bars")
    assert pick_heatmap_axes("return_to_value") == ("atr_mult_stop",
                                                     "arm_window_bars")


def test_walk_oos_curve_filters_failed_status():
    rows = [
        _row(walk_idx=0, fingerprint="a", is_sharpe=1.0, oos_pnl=100.0),
        _row(walk_idx=0, fingerprint="b", is_sharpe=5.0, oos_pnl=999.0,
             status="failed"),
    ]
    df = walk_oos_curve(_df(rows), "AAPL", "15Min", "price_discovery")
    # The failed row would have won by IS sharpe — but it's filtered out.
    assert df["oos_pnl"].tolist() == [100.0]


def test_helpers_return_empty_for_unknown_symbol():
    rows = [_row()]
    out = walk_oos_curve(_df(rows), "ZZZ", "15Min", "price_discovery")
    assert out.empty
    assert list(out.columns) == ["walk_idx", "oos_pnl", "cumulative_oos_pnl"]
