from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from backtest.wfo.grid import ParamCombo
from backtest.wfo.runner import RUNNER_RESULT_COLUMNS, RunTask, _run_one
from backtest.wfo.windowing import Walk
from core.asset_class import AssetClassConfig
from core.bar import Bar


CRYPTO = AssetClassConfig(
    name="crypto", timezone="UTC",
    session_open_local="00:00", session_close_local="23:59",
    opening_blackout_min=0, bar_timeframe="5Min",
    slippage_bps=0.0, commission_per_share=0.0, commission_bps=0.0,
)


def _flat_bars(symbol, n, base=None, c=100.0):
    base = base or datetime(2026, 1, 1, tzinfo=timezone.utc)
    return [Bar(symbol=symbol, ts=base + timedelta(minutes=5 * i),
                open=c, high=c, low=c, close=c, volume=100) for i in range(n)]


def _walk():
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return Walk(idx=0,
                is_start=base, is_end=base + timedelta(days=1),
                oos_start=base + timedelta(days=1),
                oos_end=base + timedelta(days=2))


def _combo():
    return ParamCombo(
        setup="price_discovery",
        setup_values={"enabled": True, "atr_mult_stop": 1.0, "target_R": 1.5,
                      "arm_window_bars": 6, "cooldown_bars": 12},
        pm_values={"max_hold_bars": 12, "breakeven_at_R": 1.0},
        fingerprint="abc123",
    )


def _task(*, is_bars, oos_bars, combo=None):
    return RunTask(
        symbol="BTC/USD", asset_class="crypto", timeframe="5Min",
        walk=_walk(),
        is_bars=is_bars, oos_bars=oos_bars,
        combo=combo or _combo(),
        initial_equity=100_000.0,
        ac_configs={"crypto": CRYPTO},
        risk_cfg={
            "max_risk_per_trade": 0.005, "max_notional_per_trade_pct": 0.20,
            "max_concurrent_positions": 4, "max_daily_risk_open": 0.02,
            "consecutive_loss_limit": 2, "loss_filter_scope": "per_symbol",
            "circuit_breaker": {"daily_loss_limit_1": 0.02,
                                "daily_loss_limit_2": 0.03,
                                "drawdown_limit": 0.10},
        },
        filters_cfg={"opening_blackout_min": 0, "volume_deficit_pct": 0.30},
        min_trades=1,
    )


def test_run_one_returns_full_schema():
    row = _run_one(_task(is_bars=_flat_bars("BTC/USD", 50),
                         oos_bars=_flat_bars("BTC/USD", 50)))
    assert isinstance(row, dict)
    assert set(row.keys()) >= set(RUNNER_RESULT_COLUMNS)
    assert row["symbol"] == "BTC/USD"
    assert row["asset_class"] == "crypto"
    assert row["timeframe"] == "5Min"
    assert row["walk_idx"] == 0
    assert row["fingerprint"] == "abc123"


def test_run_one_below_min_trades_status():
    # Flat bars produce zero trades; min_trades=20 forces below-floor
    task = _task(is_bars=_flat_bars("BTC/USD", 50),
                 oos_bars=_flat_bars("BTC/USD", 50))
    task.min_trades = 20
    row = _run_one(task)
    assert row["status"] == "below_min_trades"
    assert pd.isna(row["is_score"])
    assert pd.isna(row["oos_sharpe"])


def test_run_one_failed_status_on_exception(monkeypatch):
    """Force IntradayReplay to raise; verify failed-status row, no propagation."""
    import backtest.wfo.runner as runner_mod

    def boom(*args, **kwargs):
        raise RuntimeError("synthetic failure")

    monkeypatch.setattr(runner_mod, "IntradayReplay", boom)
    row = _run_one(_task(is_bars=_flat_bars("BTC/USD", 50),
                         oos_bars=_flat_bars("BTC/USD", 50)))
    assert row["status"] == "failed"
    assert "synthetic failure" in row["error"]
    assert pd.isna(row["is_sharpe"])


def test_run_one_idempotent_for_same_input():
    task = _task(is_bars=_flat_bars("BTC/USD", 50),
                 oos_bars=_flat_bars("BTC/USD", 50))
    row_a = _run_one(task)
    row_b = _run_one(task)
    # Stable across runs (Phase-8 determinism gate)
    assert row_a == row_b


from pathlib import Path

from backtest.wfo.runner import WFORunner


def _runner_cfg(*, history_start, history_end):
    return {
        "run": {"parallelism": 1, "random_seed": 42, "output_root": "runtime/wfo"},
        "history": {"start": history_start, "end": history_end,
                    "initial_equity": 100_000.0},
        "windowing": {"in_sample": "1d", "out_of_sample": "1d", "step": None},
        "timeframes": ["5Min"],
        "fitness": {"metric": "sharpe", "min_trades": 1},
        "gate": {"wfe_min": 0.5, "require_positive_oos_pnl": True},
        "grid": {
            "price_discovery": {
                "enabled": [True],
                "atr_mult_stop": [1.0, 1.5],
                "target_R": [1.5],
                "arm_window_bars": [6],
                "cooldown_bars": [12],
            },
        },
        "position_management": {"max_hold_bars": [12], "breakeven_at_R": [1.0]},
        "risk": {
            "max_risk_per_trade": 0.005, "max_notional_per_trade_pct": 0.20,
            "max_concurrent_positions": 4, "max_daily_risk_open": 0.02,
            "consecutive_loss_limit": 2, "loss_filter_scope": "per_symbol",
            "circuit_breaker": {"daily_loss_limit_1": 0.02,
                                "daily_loss_limit_2": 0.03,
                                "drawdown_limit": 0.10},
        },
        "filters": {"opening_blackout_min": 0, "volume_deficit_pct": 0.30},
    }


def test_runner_smoke_writes_results_parquet(tmp_path):
    bars = {"BTC/USD": _flat_bars("BTC/USD", n=300,
                                   base=datetime(2026, 1, 1, tzinfo=timezone.utc))}
    runner = WFORunner(
        cfg=_runner_cfg(history_start=datetime(2026, 1, 1, tzinfo=timezone.utc),
                        history_end=datetime(2026, 1, 4, tzinfo=timezone.utc)),
        asset_class_configs={"crypto": CRYPTO},
        symbols=[("BTC/USD", "crypto")],
        bars_loader=lambda sym, ac, tf: bars[sym],
        output_dir=tmp_path,
    )
    parquet_path = runner.run()
    assert parquet_path.exists()
    df = pd.read_parquet(parquet_path)
    # 2 walks × 2 combos = 4 rows
    assert len(df) == 4
    assert set(df["fingerprint"].unique()).__len__() == 2


def test_runner_skips_pair_with_empty_bars(tmp_path, caplog):
    runner = WFORunner(
        cfg=_runner_cfg(history_start=datetime(2026, 1, 1, tzinfo=timezone.utc),
                        history_end=datetime(2026, 1, 4, tzinfo=timezone.utc)),
        asset_class_configs={"crypto": CRYPTO},
        symbols=[("BTC/USD", "crypto"), ("ETH/USD", "crypto")],
        bars_loader=lambda sym, ac, tf: (
            _flat_bars(sym, 300, base=datetime(2026, 1, 1, tzinfo=timezone.utc))
            if sym == "BTC/USD" else []
        ),
        output_dir=tmp_path,
    )
    with caplog.at_level("WARNING"):
        runner.run()
    df = pd.read_parquet(tmp_path / "results.parquet")
    assert set(df["symbol"].unique()) == {"BTC/USD"}
    assert any("BARS_UNAVAILABLE" in r.message for r in caplog.records)


def test_runner_resume_skips_completed_tasks(tmp_path, monkeypatch):
    bars = {"BTC/USD": _flat_bars("BTC/USD", n=300,
                                   base=datetime(2026, 1, 1, tzinfo=timezone.utc))}
    cfg = _runner_cfg(history_start=datetime(2026, 1, 1, tzinfo=timezone.utc),
                      history_end=datetime(2026, 1, 4, tzinfo=timezone.utc))
    runner = WFORunner(
        cfg=cfg, asset_class_configs={"crypto": CRYPTO},
        symbols=[("BTC/USD", "crypto")],
        bars_loader=lambda sym, ac, tf: bars[sym],
        output_dir=tmp_path,
    )
    runner.run()
    df_first = pd.read_parquet(tmp_path / "results.parquet")
    n_first = len(df_first)

    # Wrap _run_one and count invocations.
    import backtest.wfo.runner as runner_mod
    original = runner_mod._run_one
    calls: list = []
    def counting(task):
        calls.append(task.combo.fingerprint)
        return original(task)
    monkeypatch.setattr(runner_mod, "_run_one", counting)

    runner.run()
    df_second = pd.read_parquet(tmp_path / "results.parquet")
    assert len(df_second) == n_first
    keys = list(zip(df_second["symbol"], df_second["timeframe"],
                    df_second["walk_idx"], df_second["fingerprint"]))
    assert len(keys) == len(set(keys))
    # The real resumability assertion: zero new task invocations on the second run.
    assert calls == []
