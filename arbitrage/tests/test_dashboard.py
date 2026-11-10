import json

import pandas as pd

from arbitrage.config import load_config
from arbitrage.report import build_report
from arbitrage.ui.app import load_live_state, load_report, pair_verdicts, recorder_coverage


def _results():
    return {"BTC/USDT": {"venue_buy": "A", "venue_sell": "B",
        "slippage_bps": {1.0: {"leg_risk": {0.0: {
            "median_bps": 1.5, "pct_positive": 60.0, "persistence_median_ms": 2000}}}}}}


def test_load_report_and_state(tmp_path):
    build_report(_results(), load_config(), tmp_path / "r.json", tmp_path / "r.md")
    (tmp_path / "live_state.json").write_text(json.dumps({"test_mode": True, "pnl_usd": 0.0}))
    rep = load_report(tmp_path / "r.json")
    st = load_live_state(tmp_path / "live_state.json")
    assert st["test_mode"] is True
    assert rep["BTC/USDT"]["slippage_bps"]["1.0"]["leg_risk"]["0.0"]["median_bps"] == 1.5


def test_loaders_return_none_when_missing_or_corrupt(tmp_path):
    assert load_report(tmp_path / "nope.json") is None
    assert load_live_state(tmp_path / "nope.json") is None
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert load_live_state(bad) is None
    assert load_report(bad) is None


def test_loaders_do_not_write(tmp_path):
    load_report(tmp_path / "nope.json")
    load_live_state(tmp_path / "nope.json")
    assert list(tmp_path.iterdir()) == []


def test_recorder_coverage(tmp_path):
    (tmp_path / "binance").mkdir()
    pd.DataFrame({"ts_utc_ms": [0, 1000, 11000], "bid": [1.0] * 3, "ask": [1.1] * 3}
                 ).to_parquet(tmp_path / "binance" / "BTC_USDT.parquet", index=False)
    cov = recorder_coverage(tmp_path)
    assert len(cov) == 1
    row = cov.iloc[0]
    assert row["venue"] == "binance" and row["symbol"] == "BTC_USDT"
    assert row["rows"] == 3 and row["span_s"] == 11.0
    assert recorder_coverage(tmp_path / "missing").empty


def _cell(median, pers):
    return {"leg_risk": {0.0: {"median_bps": median, "pct_positive": 60.0,
                               "persistence_median_ms": pers}}}


def test_pair_verdicts():
    cfg = load_config()
    lo, hi = cfg.go_nogo_min_slippage_bps - 0.5, cfg.go_nogo_min_slippage_bps
    pers = cfg.go_nogo_min_persistence_ms + 1
    res = {
        "REAL/USDT": {"slippage_bps": {lo: _cell(-1.0, 0), hi: _cell(1.5, pers)}},
        "BELOW/USDT": {"slippage_bps": {lo: _cell(5.0, pers)}},  # slippage too low to count
        "SHORT/USDT": {"slippage_bps": {hi: _cell(1.5, cfg.go_nogo_min_persistence_ms)}},
        "NEG/USDT": {"slippage_bps": {hi: _cell(-0.1, pers)}},
    }
    assert pair_verdicts(res, cfg) == {
        "REAL/USDT": "real", "BELOW/USDT": "not real",
        "SHORT/USDT": "not real", "NEG/USDT": "not real"}
    # works on the JSON-roundtripped report (string keys) too
    assert pair_verdicts(json.loads(json.dumps(res, default=str)), cfg) == pair_verdicts(res, cfg)
