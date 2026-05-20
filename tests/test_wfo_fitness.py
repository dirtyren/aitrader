import math

from backtest.wfo.fitness import score


def test_score_returns_sharpe_when_above_floor():
    metrics = {"sharpe": 1.5, "trades": 25}
    assert score(metrics, min_trades=20) == 1.5


def test_score_returns_none_when_below_floor():
    metrics = {"sharpe": 1.5, "trades": 10}
    assert score(metrics, min_trades=20) is None


def test_score_returns_none_when_sharpe_is_nan():
    metrics = {"sharpe": float("nan"), "trades": 30}
    assert score(metrics, min_trades=20) is None


def test_score_returns_zero_when_sharpe_zero():
    metrics = {"sharpe": 0.0, "trades": 30}
    assert score(metrics, min_trades=20) == 0.0


def test_score_handles_missing_trades_key():
    metrics = {"sharpe": 1.0}
    # Missing key → conservatively below floor
    assert score(metrics, min_trades=20) is None
