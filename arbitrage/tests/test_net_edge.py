import pandas as pd
import pytest

from arbitrage.net_edge import net_bps, net_edge_stats


def test_net_bps_formula():
    gross = pd.Series([0.5])  # quote units
    mid = pd.Series([100.0])
    # 0.5/100 * 1e4 = 50 bps; minus 10 bps cost -> 40 bps
    assert net_bps(gross, mid, 10.0).iloc[0] == pytest.approx(40.0)


def test_net_edge_stats_persistence():
    net = pd.Series([1.0, 2.0, -1.0, 3.0, 4.0, 5.0])
    s = net_edge_stats(net, grid_ms=10)
    assert s["pct_positive"] == pytest.approx(5 / 6 * 100)
    assert s["persistence_max_ms"] == 30  # three consecutive positives * 10ms
    assert s["median_bps"] == pytest.approx(2.5)  # brief said 2.0; true median is 2.5
    assert s["mean_bps"] == pytest.approx(14 / 6)
    assert s["persistence_median_ms"] == pytest.approx(25.0)  # runs 2,3 -> 2.5 * 10


def test_net_edge_stats_empty_and_all_negative():
    for net in (pd.Series([], dtype=float), pd.Series([-1.0, -2.0])):
        s = net_edge_stats(net, grid_ms=10)
        assert s["persistence_median_ms"] == 0
        assert s["persistence_max_ms"] == 0
        assert s["pct_positive"] == 0
