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


import yaml

from backtest.wfo.report import emit_live_overrides, emit_summary_md


def _agg_passing():
    return pd.DataFrame([
        {"symbol": "AAPL", "timeframe": "15Min", "setup": "price_discovery",
         "walks": 30, "sum_is_sharpe": 30.0, "sum_oos_sharpe": 22.0,
         "wfe": 0.733, "total_oos_pnl": 4_213.5, "mean_oos_sharpe": 0.733,
         "passed": True, "winning_fingerprint_last_walk": "fp_apl"},
        {"symbol": "AAPL", "timeframe": "30Min", "setup": "vwap_bounce",
         "walks": 30, "sum_is_sharpe": 28.0, "sum_oos_sharpe": 14.0,
         "wfe": 0.5, "total_oos_pnl": 2_000.0, "mean_oos_sharpe": 0.467,
         "passed": True, "winning_fingerprint_last_walk": "fp_avb"},
        {"symbol": "TSLA", "timeframe": "5Min", "setup": "price_discovery",
         "walks": 30, "sum_is_sharpe": 10.0, "sum_oos_sharpe": 1.0,
         "wfe": 0.1, "total_oos_pnl": -500.0, "mean_oos_sharpe": 0.033,
         "passed": False, "winning_fingerprint_last_walk": "fp_tpd"},
    ])


def _last_walk_combos():
    """Map fingerprint → (setup_values, pm_values)."""
    return {
        "fp_apl": ({"atr_mult_stop": 1.25, "target_R": 2.0, "arm_window_bars": 6},
                   {"max_hold_bars": 12, "breakeven_at_R": 1.0}),
        "fp_avb": ({"atr_mult_stop": 1.5, "target_R": 2.5, "arm_window_bars": 4},
                   {"max_hold_bars": 8, "breakeven_at_R": 0.75}),
        "fp_tpd": ({"atr_mult_stop": 1.0}, {"max_hold_bars": 12, "breakeven_at_R": 1.0}),
    }


def test_emit_live_overrides_picks_highest_oos_sharpe_per_symbol(tmp_path):
    out_path = tmp_path / "live_overrides.yaml"
    emit_live_overrides(_agg_passing(), _last_walk_combos(), out_path,
                        run_id="2026-05-19T00-00_test", git_sha="b273796",
                        gate=GateConfig(wfe_min=0.5))
    data = yaml.safe_load(out_path.read_text())
    # AAPL → 15Min wins (mean_oos_sharpe 0.733 > 0.467)
    assert data["symbols"]["AAPL"]["timeframe"] == "15Min"
    assert data["symbols"]["AAPL"]["setup"] == "price_discovery"
    assert data["symbols"]["AAPL"]["setup_params"]["target_R"] == 2.0
    # TSLA failed → not present
    assert "TSLA" not in data["symbols"]


def test_emit_live_overrides_empty_when_none_pass(tmp_path):
    df = _agg_passing().assign(passed=False)
    out_path = tmp_path / "live_overrides.yaml"
    emit_live_overrides(df, _last_walk_combos(), out_path,
                        run_id="r", git_sha="s",
                        gate=GateConfig(wfe_min=0.5))
    data = yaml.safe_load(out_path.read_text())
    assert data["symbols"] == {}


def test_emit_summary_md_contains_all_groups(tmp_path):
    out_path = tmp_path / "summary.md"
    emit_summary_md(_agg_passing(), out_path,
                    run_id="r", git_sha="s",
                    gate=GateConfig(wfe_min=0.5))
    text = out_path.read_text()
    assert "AAPL" in text and "TSLA" in text
    assert "price_discovery" in text
    assert "passed" in text.lower()


def test_update_latest_symlink_atomic(tmp_path):
    from backtest.wfo.report import update_latest_symlink
    runs = tmp_path / "runs"
    run1 = runs / "run1"
    run2 = runs / "run2"
    run1.mkdir(parents=True)
    run2.mkdir(parents=True)
    latest = runs / "latest"

    update_latest_symlink(latest, run1)
    assert latest.is_symlink()
    assert latest.resolve() == run1.resolve()

    update_latest_symlink(latest, run2)
    assert latest.resolve() == run2.resolve()


def test_update_latest_symlink_skipped_when_zero_passed(tmp_path):
    from backtest.wfo.report import update_latest_symlink_if_passing
    runs = tmp_path / "runs"
    run1 = runs / "run1"
    run1.mkdir(parents=True)
    latest = runs / "latest"

    aggregated = _agg_passing().assign(passed=False)
    update_latest_symlink_if_passing(latest, run1, aggregated)
    assert not latest.exists()
