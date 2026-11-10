from dataclasses import replace
from datetime import date

import pandas as pd
import pytest

from arbitrage.config import ExchangeConfig, load_config
from arbitrage.connectors.base import VenueConnector
from arbitrage.measure import main, measure_pair


class FakeConnector(VenueConnector):
    def __init__(self, frame):
        self.frame = frame
        self.calls = []

    def load_quotes(self, symbol, start, end):
        self.calls.append((symbol, start, end))
        return self.frame


def _frames():
    # venue A ask=100.0, venue B bid=100.05 -> gross = 0.05 -> 5 bps.
    # A is a locked book (bid == ask == 100.0) so A's mid is exactly 100.0 and
    # the bps-of-mid conversion recovers exactly 5.0 (see test_bps_is_of_buy_venue_mid).
    a = pd.DataFrame({"ts_utc_ms": [0, 10, 20], "bid": [100.0] * 3, "ask": [100.0] * 3})
    b = pd.DataFrame({"ts_utc_ms": [0, 10, 20], "bid": [100.05] * 3, "ask": [100.15] * 3})
    return a, b


def _cfg(fee_a=0.0, fee_b=0.0, rebal=0.0, adverse=0.0):
    return replace(
        load_config(),
        exchanges={"A": ExchangeConfig("A", fee_a), "B": ExchangeConfig("B", fee_b)},
        rebalancing_bps=rebal,
        adverse_move_bps=adverse,
    )


def _run(cfg, a, b):
    return measure_pair(
        "X", cfg, {"A": FakeConnector(a), "B": FakeConnector(b)}, date.today(), date.today()
    )


def test_recovers_injected_spread_exactly():
    a, b = _frames()
    res = _run(_cfg(), a, b)
    # buy on A (ask 100.0), sell on B (bid 100.05) -> gross 5 bps, zero cost -> net 5 bps
    assert res["venue_buy"] == "A" and res["venue_sell"] == "B"
    assert res["slippage_bps"][0.0]["leg_risk"][0.0]["median_bps"] == pytest.approx(5.0)


def test_bps_is_of_buy_venue_mid():
    a, b = _frames()
    a["bid"] = 99.9  # mid_A = 99.95, gross 0.05 -> 0.05 / 99.95 * 1e4 bps
    res = _run(_cfg(), a, b)
    assert res["slippage_bps"][0.0]["leg_risk"][0.0]["median_bps"] == pytest.approx(0.05 / 99.95 * 1e4)


def test_costs_reduce_edge_by_exact_amount():
    a, b = _frames()
    res = _run(_cfg(fee_a=1.0, fee_b=0.5, rebal=0.25, adverse=4.0), a, b)
    # slippage 2, leg_risk 0.5 -> cost = 1 + 0.5 + 2 + 0.5*4 + 0.25 = 5.75
    cell = res["slippage_bps"][2.0]["leg_risk"][0.5]
    assert cell["median_bps"] == pytest.approx(5.0 - 5.75)
    assert cell["pct_positive"] == 0.0


def test_sweeps_full_grid():
    a, b = _frames()
    cfg = _cfg()
    res = _run(cfg, a, b)
    assert tuple(res["slippage_bps"]) == cfg.slippage_bps_grid
    for sdata in res["slippage_bps"].values():
        assert tuple(sdata["leg_risk"]) == cfg.leg_risk_grid
        for st in sdata["leg_risk"].values():
            assert {"median_bps", "pct_positive", "persistence_median_ms"} <= set(st)


def test_picks_better_direction():
    a, b = _frames()
    # swap venues: now buying on B / selling on A is the profitable direction
    res = _run(_cfg(), b, a)
    assert (res["venue_buy"], res["venue_sell"]) == ("B", "A")
    assert res["slippage_bps"][0.0]["leg_risk"][0.0]["median_bps"] == pytest.approx(5.0)


def test_no_data_gives_nan_not_crash():
    empty = pd.DataFrame({"ts_utc_ms": [], "bid": [], "ask": []})
    a, _ = _frames()
    res = _run(_cfg(), a, empty)
    assert pd.isna(res["slippage_bps"][0.0]["leg_risk"][0.0]["median_bps"])


def test_loads_one_day_window_per_day():
    a, b = _frames()
    ca, cb = FakeConnector(a), FakeConnector(b)
    measure_pair("X", _cfg(), {"A": ca, "B": cb}, date(2024, 1, 1), date(2024, 1, 3))
    assert len(ca.calls) == len(cb.calls) == 3
    sym, s, e = ca.calls[0]
    assert (s.date(), e.date()) == (date(2024, 1, 1), date(2024, 1, 1))
    assert s.tzinfo is not None and e > s


def test_requires_exactly_two_venues():
    a, _ = _frames()
    with pytest.raises(ValueError):
        measure_pair("X", _cfg(), {"A": FakeConnector(a)}, date.today(), date.today())


def test_cli_rejects_single_venue():
    with pytest.raises(SystemExit):
        main(["--pair", "BTC/USDT", "--venues", "binance", "--start", "2024-01-01", "--end", "2024-01-01"])
