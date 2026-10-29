"""Unit tests for Dual-Momentum Tactical ETF Rotation Strategy."""

from datetime import date, datetime
import numpy as np
import pandas as pd
import pytest

from strategies.dual_momentum_etf import (
    DualMomentumConfig,
    DualMomentumEngine,
    RegimeState,
    RebalanceTrigger,
    apply_turnover_buffer,
    compute_relative_momentum_scores,
    compute_single_momentum_score,
    compute_target_weights,
    is_friday,
    is_last_friday_of_month,
    is_trimester_rebalance_date,
)


def test_single_momentum_score_math():
    """Verify MomentumScore = 0.60 * R12W + 0.40 * R24W."""
    # 121 bars so index 0 is t-120, index 60 is t-60, index 120 is t
    closes = [100.0] * 121
    # P_{t-120} = 100.0
    closes[0] = 100.0
    # P_{t-60} = 110.0 -> R12W = (132 / 110) - 1 = 0.20
    closes[60] = 110.0
    # P_t = 132.0 -> R24W = (132 / 100) - 1 = 0.32
    closes[-1] = 132.0

    score = compute_single_momentum_score(closes, lookback_12w=60, lookback_24w=120, w1=0.60, w2=0.40)
    expected = 0.60 * (132.0 / 110.0 - 1.0) + 0.40 * (132.0 / 100.0 - 1.0)
    assert pytest.approx(expected, 1e-6) == score


def test_insufficient_history_returns_neg_inf():
    closes = [100.0] * 50
    score = compute_single_momentum_score(closes, lookback_12w=60, lookback_24w=120)
    assert score == -np.inf


def test_relative_momentum_scores_ranking():
    """Higher score gets lower rank index (first in list)."""
    n = 130
    prices = {
        "SMGB": [100.0 + i * 1.5 for i in range(n)],  # Strongest
        "QDVE": [100.0 + i * 1.0 for i in range(n)],  # Second
        "EQQQ": [100.0 + i * 0.5 for i in range(n)],  # Third
        "ISPY": [100.0 + i * 0.2 for i in range(n)],  # Fourth
    }
    df = pd.DataFrame(prices)
    scores = compute_relative_momentum_scores(df, lookback_12w=60, lookback_24w=120)

    ranked_symbols = list(scores.index)
    assert ranked_symbols == ["SMGB", "QDVE", "EQQQ", "ISPY"]
    assert scores["SMGB"] > scores["QDVE"] > scores["EQQQ"] > scores["ISPY"]


def test_calendar_helpers():
    # 2026-03-27 is the last Friday of March 2026
    d_mar = date(2026, 3, 27)
    assert is_friday(d_mar)
    assert is_last_friday_of_month(d_mar)
    assert is_trimester_rebalance_date(d_mar)

    # 2026-03-20 is a Friday, but not the last Friday of March
    d_mar_prev = date(2026, 3, 20)
    assert is_friday(d_mar_prev)
    assert not is_last_friday_of_month(d_mar_prev)
    assert not is_trimester_rebalance_date(d_mar_prev)

    # 2026-06-26 is the last Friday of June 2026
    d_jun = date(2026, 6, 26)
    assert is_trimester_rebalance_date(d_jun)

    # 2026-09-25 is the last Friday of September 2026
    d_sep = date(2026, 9, 25)
    assert is_trimester_rebalance_date(d_sep)

    # 2026-12-25 is the last Friday of December 2026
    d_dec = date(2026, 12, 25)
    assert is_trimester_rebalance_date(d_dec)

    # 2026-04-24 is the last Friday of April (not a quarter month)
    d_apr = date(2026, 4, 24)
    assert is_last_friday_of_month(d_apr)
    assert not is_trimester_rebalance_date(d_apr)


def test_turnover_buffer_retains_incumbent():
    """If #3 and #4 differ by < 1.0% (0.01), retain incumbent."""
    # Rank 1: SMGB, Rank 2: QDVE, Rank 3: EQQQ (score 0.250), Rank 4: ISPY (score 0.245)
    # Difference between #3 and #4 is 0.005 (< 0.01)
    candidates = [
        ("SMGB", 0.35),
        ("QDVE", 0.30),
        ("EQQQ", 0.250),
        ("ISPY", 0.245),
        ("XUFN", 0.10),
    ]

    # Case A: ISPY is currently held (incumbent). Buffer rule retains ISPY at rank #3!
    buffered = apply_turnover_buffer(candidates, incumbent_holdings={"ISPY"}, target_count=3, threshold=0.01)
    top_3 = [sym for sym, _ in buffered[:3]]
    assert top_3 == ["SMGB", "QDVE", "ISPY"]
    assert buffered[3][0] == "EQQQ"

    # Case B: Difference >= 0.01. No retention, new leader takes rank #3.
    candidates_large_diff = [
        ("SMGB", 0.35),
        ("QDVE", 0.30),
        ("EQQQ", 0.260),  # diff = 0.015 >= 0.01
        ("ISPY", 0.245),
        ("XUFN", 0.10),
    ]
    buffered_no_swap = apply_turnover_buffer(
        candidates_large_diff, incumbent_holdings={"ISPY"}, target_count=3, threshold=0.01
    )
    top_3_no_swap = [sym for sym, _ in buffered_no_swap[:3]]
    assert top_3_no_swap == ["SMGB", "QDVE", "EQQQ"]

    # Case C: EQQQ is already held. Stays in top 3.
    buffered_eqqq_held = apply_turnover_buffer(
        candidates, incumbent_holdings={"EQQQ"}, target_count=3, threshold=0.01
    )
    assert [sym for sym, _ in buffered_eqqq_held[:3]] == ["SMGB", "QDVE", "EQQQ"]


def test_target_weights_allocation():
    cfg = DualMomentumConfig()
    ranked = ["SMGB", "QDVE", "EQQQ", "ISPY"]

    # Normal Bull Mode (VIX < 22): 35% / 35% / 20% / 10% Cash
    w_normal = compute_target_weights(RegimeState.RISK_ON_NORMAL, ranked, cfg)
    assert w_normal["SMGB"] == 0.35
    assert w_normal["QDVE"] == 0.35
    assert w_normal["EQQQ"] == 0.20
    assert w_normal["ISPY"] == 0.0
    assert w_normal["CSH2"] == 0.10
    assert sum(w_normal.values()) == pytest.approx(1.0)

    # Defensive De-risking (22 <= VIX < 32): 25% / 25% / 50% Cash
    w_def = compute_target_weights(RegimeState.RISK_ON_DEFENSIVE, ranked, cfg)
    assert w_def["SMGB"] == 0.25
    assert w_def["QDVE"] == 0.25
    assert w_def["EQQQ"] == 0.0
    assert w_def["CSH2"] == 0.50
    assert sum(w_def.values()) == pytest.approx(1.0)

    # Risk-Off: 100% Cash
    w_off = compute_target_weights(RegimeState.RISK_OFF, ranked, cfg)
    assert w_off["CSH2"] == 1.0
    assert all(w_off[sym] == 0.0 for sym in cfg.satellite_symbols)


def test_engine_trimester_rebalance():
    cfg = DualMomentumConfig()
    engine = DualMomentumEngine(cfg)

    # Mock historical ETF prices
    n = 150
    dates = pd.date_range("2025-10-01", periods=n, freq="B")
    data = {}
    for sym in cfg.satellite_symbols:
        data[sym] = [100.0 + i for i in range(n)]
    df = pd.DataFrame(data, index=dates)

    # Trimester date: 2026-03-27 (Last Friday of March)
    eval_date = date(2026, 3, 27)

    # Benchmark: S&P 500 > 200 SMA, VIX = 16 (< 22)
    # Consecutive days above SMA: simulate prior days above SMA
    engine.consecutive_above_sma = 5
    engine.regime = RegimeState.RISK_OFF

    # Re-entry hysteresis requires 3 consecutive days above SMA AND VIX < 25
    res = engine.evaluate_step(
        eval_date=eval_date,
        prices_history=df,
        benchmark_close=5000.0,
        benchmark_sma=4800.0,
        vix_value=16.0,
        is_quarter_rebalance=True,
        is_friday_check=True,
    )

    assert res.rebalance_triggered is True
    assert res.regime == RegimeState.RISK_ON_NORMAL
    assert res.target_weights[cfg.safe_harbor_symbol] == 0.10


def test_engine_weekly_risk_off_circuit_breaker():
    cfg = DualMomentumConfig()
    engine = DualMomentumEngine(cfg)
    engine.regime = RegimeState.RISK_ON_NORMAL
    engine.current_holdings = {"SMGB", "QDVE", "EQQQ"}

    n = 150
    df = pd.DataFrame({sym: [100.0] * n for sym in cfg.satellite_symbols})

    # Scenario 1: S&P closes below 200 SMA for 2 consecutive days
    friday = date(2026, 5, 15)  # A Friday
    # First day below SMA
    engine.update_daily_streaks(benchmark_close=4700.0, benchmark_sma=4800.0)
    assert engine.consecutive_below_sma == 1

    # Second day below SMA (Friday check)
    res = engine.evaluate_step(
        eval_date=friday,
        prices_history=df,
        benchmark_close=4690.0,
        benchmark_sma=4800.0,
        vix_value=20.0,
        is_quarter_rebalance=False,
        is_friday_check=True,
    )

    assert res.rebalance_triggered is True
    assert res.trigger_type == RebalanceTrigger.CIRCUIT_BREAKER
    assert res.regime == RegimeState.RISK_OFF
    assert res.target_weights[cfg.safe_harbor_symbol] == 1.0

    # Scenario 2: VIX spikes >= 32
    engine.reset(initial_regime=RegimeState.RISK_ON_NORMAL)
    res_vix = engine.evaluate_step(
        eval_date=friday,
        prices_history=df,
        benchmark_close=5000.0,
        benchmark_sma=4800.0,
        vix_value=33.5,  # Crisis VIX >= 32
        is_quarter_rebalance=False,
        is_friday_check=True,
    )
    assert res_vix.rebalance_triggered is True
    assert res_vix.trigger_type == RebalanceTrigger.CIRCUIT_BREAKER
    assert res_vix.regime == RegimeState.RISK_OFF
    assert res_vix.target_weights[cfg.safe_harbor_symbol] == 1.0


def test_engine_reentry_hysteresis_rule():
    """Requires 3 consecutive days above SMA200 AND VIX < 25."""
    cfg = DualMomentumConfig()
    engine = DualMomentumEngine(cfg)
    engine.regime = RegimeState.RISK_OFF

    n = 150
    df = pd.DataFrame({sym: [100.0] * n for sym in cfg.satellite_symbols})

    # Day 1 above SMA: should NOT re-enter
    res1 = engine.evaluate_step(
        eval_date=date(2026, 6, 1),
        prices_history=df,
        benchmark_close=4850.0,
        benchmark_sma=4800.0,
        vix_value=20.0,
        is_quarter_rebalance=False,
        is_friday_check=False,
    )
    assert res1.regime == RegimeState.RISK_OFF
    assert res1.rebalance_triggered is False
    assert engine.consecutive_above_sma == 1

    # Day 2 above SMA: should NOT re-enter
    res2 = engine.evaluate_step(
        eval_date=date(2026, 6, 2),
        prices_history=df,
        benchmark_close=4860.0,
        benchmark_sma=4800.0,
        vix_value=20.0,
        is_quarter_rebalance=False,
        is_friday_check=False,
    )
    assert res2.regime == RegimeState.RISK_OFF
    assert res2.rebalance_triggered is False
    assert engine.consecutive_above_sma == 2

    # Day 3 above SMA, but VIX = 26 (>= 25): should NOT re-enter
    res3_high_vix = engine.evaluate_step(
        eval_date=date(2026, 6, 3),
        prices_history=df,
        benchmark_close=4870.0,
        benchmark_sma=4800.0,
        vix_value=26.0,
        is_quarter_rebalance=False,
        is_friday_check=False,
    )
    assert res3_high_vix.regime == RegimeState.RISK_OFF
    assert res3_high_vix.rebalance_triggered is False
    assert engine.consecutive_above_sma == 3

    # Day 4: Still above SMA, and VIX drops to 21 (< 22): RE-ENTERS NORMAL BULL MODE!
    res4 = engine.evaluate_step(
        eval_date=date(2026, 6, 4),
        prices_history=df,
        benchmark_close=4880.0,
        benchmark_sma=4800.0,
        vix_value=21.0,
        is_quarter_rebalance=False,
        is_friday_check=False,
    )
    assert res4.regime == RegimeState.RISK_ON_NORMAL
    assert res4.rebalance_triggered is True
    assert res4.trigger_type == RebalanceTrigger.HYSTERESIS_REENTRY
    assert res4.target_weights[cfg.safe_harbor_symbol] == 0.10
