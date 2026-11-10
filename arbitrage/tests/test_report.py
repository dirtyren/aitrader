import json

from arbitrage.config import load_config
from arbitrage.report import build_report, go_nogo


def test_go_nogo_thresholds():
    assert go_nogo(1.5, 2000, 1.0, 1000) == "real"
    assert go_nogo(0.5, 2000, 1.0, 1000) == "not real"
    assert go_nogo(1.5, 500, 1.0, 1000) == "not real"
    assert go_nogo(1.0, 1000, 1.0, 1000) == "real"  # thresholds inclusive
    assert go_nogo(float("nan"), 2000, 1.0, 1000) == "not real"


def _results():
    return {"BTC/USDT": {"venue_buy": "A", "venue_sell": "B",
        "slippage_bps": {1.0: {"leg_risk": {0.0: {
            "median_bps": 1.5, "pct_positive": 60.0, "persistence_median_ms": 2000}}}}}}


def test_build_report_writes_files(tmp_path):
    build_report(_results(), load_config(), tmp_path / "r.json", tmp_path / "r.md")
    assert (tmp_path / "r.json").exists() and (tmp_path / "r.md").exists()
    assert "real" in (tmp_path / "r.md").read_text()


def test_build_report_content(tmp_path):
    res = _results()
    res["BTC/USDT"]["slippage_bps"][1.0]["leg_risk"][5.0] = {
        "median_bps": -0.2, "pct_positive": 10.0, "persistence_median_ms": 50}
    build_report(res, load_config(), tmp_path / "r.json", tmp_path / "r.md")
    md = (tmp_path / "r.md").read_text()
    assert "BTC/USDT" in md and "| 1.5" in md
    assert "BTC/USDT slippage=1.0 leg_risk=0.0: real" in md
    assert "BTC/USDT slippage=1.0 leg_risk=5.0: not real" in md
    assert md.index("leg_risk=0.0: real") < md.index("| median_bps")
    data = json.loads((tmp_path / "r.json").read_text())
    assert data["BTC/USDT"]["slippage_bps"]["1.0"]["leg_risk"]["0.0"]["median_bps"] == 1.5
