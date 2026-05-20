import math

import pandas as pd

from backtest.wfo.report import GateConfig, aggregate_results


def _row(symbol="AAPL", tf="5Min", walk=0, setup="price_discovery",
         fingerprint="fp1", *,
         is_sharpe, oos_sharpe, oos_pnl=100.0, oos_trades=10,
         status="ok"):
    return {
        "symbol": symbol, "asset_class": "us_equity", "timeframe": tf,
        "walk_idx": walk, "setup": setup, "fingerprint": fingerprint,
        "combo_values_json": "{}",
        "is_sharpe": is_sharpe, "is_trades": 25, "is_pnl": 0.0,
        "is_score": is_sharpe,
        "oos_sharpe": oos_sharpe, "oos_trades": oos_trades, "oos_pnl": oos_pnl,
        "oos_max_dd": 0.05, "oos_avg_R": 1.0,
        "status": status, "error": "",
    }


def test_aggregate_picks_per_walk_is_best():
    df = pd.DataFrame([
        # walk 0 — fp_a wins IS
        _row(walk=0, fingerprint="fp_a", is_sharpe=2.0, oos_sharpe=1.5),
        _row(walk=0, fingerprint="fp_b", is_sharpe=1.0, oos_sharpe=0.5),
        # walk 1 — fp_b wins IS
        _row(walk=1, fingerprint="fp_a", is_sharpe=0.8, oos_sharpe=0.3),
        _row(walk=1, fingerprint="fp_b", is_sharpe=1.5, oos_sharpe=1.0),
    ])
    out = aggregate_results(df, GateConfig(wfe_min=0.5, require_positive_oos_pnl=True))
    assert len(out) == 1
    row = out.iloc[0]
    # WFE = (1.5 + 1.0) / (2.0 + 1.5) = 2.5/3.5 ≈ 0.714
    assert abs(row["wfe"] - (2.5 / 3.5)) < 1e-9
    assert row["passed"] is True


def test_aggregate_gate_fails_when_wfe_below_min():
    df = pd.DataFrame([
        _row(walk=0, is_sharpe=2.0, oos_sharpe=0.3),
        _row(walk=1, is_sharpe=2.0, oos_sharpe=0.3),
    ])
    out = aggregate_results(df, GateConfig(wfe_min=0.5, require_positive_oos_pnl=True))
    # WFE = 0.6/4.0 = 0.15 → fail
    assert out.iloc[0]["passed"] is False


def test_aggregate_gate_fails_on_negative_oos_pnl():
    df = pd.DataFrame([
        _row(walk=0, is_sharpe=2.0, oos_sharpe=1.5, oos_pnl=-50.0),
    ])
    out = aggregate_results(df, GateConfig(wfe_min=0.5, require_positive_oos_pnl=True))
    assert out.iloc[0]["passed"] is False


def test_aggregate_handles_zero_or_negative_is_sharpe_sum():
    df = pd.DataFrame([
        _row(walk=0, is_sharpe=-1.0, oos_sharpe=0.5),
        _row(walk=1, is_sharpe=-1.0, oos_sharpe=0.5),
    ])
    out = aggregate_results(df, GateConfig(wfe_min=0.5, require_positive_oos_pnl=True))
    # Σ IS Sharpe ≤ 0 → wfe = NaN, gate fails
    assert math.isnan(out.iloc[0]["wfe"])
    assert out.iloc[0]["passed"] is False


def test_aggregate_ignores_non_ok_status():
    df = pd.DataFrame([
        _row(walk=0, is_sharpe=2.0, oos_sharpe=1.5),
        _row(walk=0, fingerprint="other", is_sharpe=99.0, oos_sharpe=99.0,
             status="failed"),
        _row(walk=1, is_sharpe=1.5, oos_sharpe=1.0),
    ])
    out = aggregate_results(df, GateConfig(wfe_min=0.5, require_positive_oos_pnl=True))
    # Failed row must be excluded; same expected WFE as the 2-walk happy path
    assert abs(out.iloc[0]["wfe"] - (2.5 / 3.5)) < 1e-9


def test_aggregate_emits_one_row_per_setup_timeframe():
    df = pd.DataFrame([
        _row(setup="price_discovery", walk=0, is_sharpe=1.0, oos_sharpe=0.7),
        _row(setup="price_discovery", walk=1, is_sharpe=1.0, oos_sharpe=0.7),
        _row(setup="vwap_bounce", fingerprint="fp_v", walk=0,
             is_sharpe=2.0, oos_sharpe=1.5),
        _row(setup="vwap_bounce", fingerprint="fp_v", walk=1,
             is_sharpe=2.0, oos_sharpe=1.5),
    ])
    out = aggregate_results(df, GateConfig(wfe_min=0.5, require_positive_oos_pnl=True))
    assert set(out["setup"]) == {"price_discovery", "vwap_bounce"}
