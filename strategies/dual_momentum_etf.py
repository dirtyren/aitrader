"""Dual-Momentum Tactical ETF Rotation Strategy with a 200-Day SMA & Volatility Regime Filter.

Specification:
1. Universe:
   - High-Beta Momentum Candidates: SMGB, QDVE, EQQQ, ISPY, XUFN, SXLE, XUIN (UCITS ETFs).
   - Safe Harbor Assets: CSH2 / FEDF / XEON (Cash equivalents).
   - Macro Regime Reference: S&P 500 Index (or CSPX / SPY) and CBOE VIX.

2. Quantitative Scoring:
   - MomentumScore = 0.60 * R12W + 0.40 * R24W
     where R12W = P_t / P_{t-60} - 1 (60 trading days)
           R24W = P_t / P_{t-120} - 1 (120 trading days)
   - Macro Benchmark Trend: S&P 500 Close > SMA_200.

3. Dynamic Position Sizing Matrix:
   - S&P 500 > 200d SMA and VIX < 22:
       100% Risk-On (90% Equity, 10% Cash): Top 1 (35%), Top 2 (35%), Top 3 (20%), Cash (10%).
   - S&P 500 > 200d SMA and 22.0 <= VIX < 32.0:
       50% Defensive De-Risking (50% Equity, 50% Cash): Top 1 (25%), Top 2 (25%), Cash (50%).
   - S&P 500 > 200d SMA and VIX >= 32.0:
       100% Cash / Overnight Liquidity (CSH2/XEON).
   - S&P 500 <= 200d SMA (Any VIX):
       100% Cash / Overnight Liquidity (CSH2/XEON).

4. Operational Calendar & Defensive Circuit Breaker:
   - Cadence 1 (Trimester Rebalance): Last Friday of March, June, September, December.
   - Cadence 2 (Weekly Risk-Off Check): Every Friday at 3:45 PM EST.
       Triggers 100% Cash if:
         a) S&P 500 closed below 200-day SMA for 2 consecutive trading sessions, OR
         b) VIX >= 32.
   - Re-Entry Hysteresis Rule:
       Do NOT re-enter on first green day.
       Require S&P 500 to close above 200d SMA for 3 consecutive trading days AND VIX < 25.

5. Slippage & Buffer Rules:
   - Turnover Buffer Rule: If #3 and #4 ranked ETFs differ in momentum score by < 1.0% (0.01),
     keep incumbent holding.
   - Limit orders placed at bid/ask midpoint 15-30 minutes after open (10:00 AM EST).
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Any, Optional

import numpy as np
import pandas as pd


class RegimeState(str, Enum):
    RISK_ON_NORMAL = "RISK_ON_NORMAL"          # S&P > 200 SMA, VIX < 22
    RISK_ON_DEFENSIVE = "RISK_ON_DEFENSIVE"    # S&P > 200 SMA, 22 <= VIX < 32
    RISK_OFF = "RISK_OFF"                      # S&P <= 200 SMA or VIX >= 32


class RebalanceTrigger(str, Enum):
    TRIMESTER = "trimester_rebalance"
    CIRCUIT_BREAKER = "circuit_breaker_risk_off"
    HYSTERESIS_REENTRY = "hysteresis_reentry"
    VOLATILITY_DERISK = "volatility_derisk"
    VOLATILITY_RERISK = "volatility_rerisk"
    NONE = "none"


@dataclass(frozen=True)
class DualMomentumConfig:
    # Lookback windows in trading days
    lookback_12w: int = 60
    lookback_24w: int = 120
    weight_12w: float = 0.60
    weight_24w: float = 0.40
    sma_period: int = 200

    # Volatility thresholds
    vix_defensive_threshold: float = 22.0
    vix_crisis_threshold: float = 32.0
    vix_reentry_threshold: float = 25.0

    # Consecutive days rules
    consecutive_below_sma_exit: int = 2
    consecutive_above_sma_reenter: int = 3

    # Turnover buffer threshold (1.0% = 0.01)
    turnover_buffer_threshold: float = 0.01

    # Weights allocations
    # Normal Bull Mode (90% Equity, 10% Cash)
    weight_normal_top1: float = 0.35
    weight_normal_top2: float = 0.35
    weight_normal_top3: float = 0.20
    weight_normal_cash: float = 0.10

    # Defensive De-Risking (50% Equity, 50% Cash)
    weight_defensive_top1: float = 0.25
    weight_defensive_top2: float = 0.25
    weight_defensive_cash: float = 0.50

    # Instruments
    satellite_symbols: list[str] = field(default_factory=lambda: [
        "SMGB", "QDVE", "EQQQ", "ISPY", "XUFN", "SXLE", "XUIN"
    ])
    safe_harbor_symbol: str = "CSH2"
    benchmark_symbol: str = "SPY"
    vix_symbol: str = "VIX"

    # Rebalance months (March, June, September, December)
    rebalance_months: tuple[int, ...] = (3, 6, 9, 12)


# ---------------------------------------------------------------------------
# Calendar Helpers
# ---------------------------------------------------------------------------

def is_last_friday_of_month(d: date) -> bool:
    """Return True if `d` is the last Friday of its month."""
    if d.weekday() != 4:  # 4 is Friday
        return False
    # Check if adding 7 days moves into the next month
    # or if any later date in the same month is also a Friday
    _, last_day = calendar.monthrange(d.year, d.month)
    return (d.day + 7) > last_day


def is_trimester_rebalance_date(d: date | datetime, rebalance_months: tuple[int, ...] = (3, 6, 9, 12)) -> bool:
    """Check if date `d` is the last Friday of March, June, September, or December."""
    if isinstance(d, datetime):
        d = d.date()
    return d.month in rebalance_months and is_last_friday_of_month(d)


def is_friday(d: date | datetime) -> bool:
    """Check if date `d` is Friday."""
    if isinstance(d, datetime):
        d = d.date()
    return d.weekday() == 4


# ---------------------------------------------------------------------------
# Quantitative Momentum Scoring
# ---------------------------------------------------------------------------

def compute_single_momentum_score(
    closes: list[float] | np.ndarray | pd.Series,
    lookback_12w: int = 60,
    lookback_24w: int = 120,
    w1: float = 0.60,
    w2: float = 0.40,
) -> float:
    """Compute Relative Momentum Score (RMS) for a single series of close prices.

    MomentumScore = 0.60 * R12W + 0.40 * R24W
    Where:
        R12W = P_t / P_{t-60} - 1
        R24W = P_t / P_{t-120} - 1
    """
    if isinstance(closes, pd.Series):
        arr = closes.to_numpy(dtype=float)
    else:
        arr = np.asarray(closes, dtype=float)

    if len(arr) < lookback_24w + 1:
        return -np.inf

    p_t = float(arr[-1])
    p_t_60 = float(arr[-1 - lookback_12w])
    p_t_120 = float(arr[-1 - lookback_24w])

    if p_t_60 <= 0 or p_t_120 <= 0 or np.isnan(p_t) or np.isnan(p_t_60) or np.isnan(p_t_120):
        return -np.inf

    r12w = (p_t / p_t_60) - 1.0
    r24w = (p_t / p_t_120) - 1.0

    return (w1 * r12w) + (w2 * r24w)


def compute_relative_momentum_scores(
    prices_df: pd.DataFrame,
    lookback_12w: int = 60,
    lookback_24w: int = 120,
    w1: float = 0.60,
    w2: float = 0.40,
) -> pd.Series:
    """Compute Relative Momentum Scores for all ETF columns at the latest timestamp.

    Returns a pd.Series indexed by ETF symbol, sorted descending by momentum score.
    """
    scores = {}
    for col in prices_df.columns:
        series = prices_df[col].dropna()
        scores[col] = compute_single_momentum_score(
            series, lookback_12w=lookback_12w, lookback_24w=lookback_24w, w1=w1, w2=w2
        )
    return pd.Series(scores).sort_values(ascending=False)


def compute_sma_200(series: pd.Series, period: int = 200) -> pd.Series:
    """Compute 200-day Simple Moving Average."""
    return series.rolling(window=period, min_periods=period).mean()


# ---------------------------------------------------------------------------
# Turnover Buffer Rule
# ---------------------------------------------------------------------------

def apply_turnover_buffer(
    ranked_candidates: list[tuple[str, float]],
    incumbent_holdings: set[str],
    target_count: int = 3,
    threshold: float = 0.01,
) -> list[tuple[str, float]]:
    """Apply the turnover buffer rule to candidate rankings.

    Rule:
    If the ETF at cutoff rank (e.g. #3) and the next-ranked ETF (e.g. #4)
    differ in momentum score by less than 1.0% (0.01), do not trade.
    Keep the incumbent holding to save on broker commissions, bid-ask spreads,
    and unnecessary tax events.

    Parameters:
        ranked_candidates: list of (symbol, momentum_score) sorted descending
        incumbent_holdings: set of symbols currently held in the portfolio
        target_count: number of ETFs to select (3 in normal mode, 2 in defensive)
        threshold: minimum score differential required to replace an incumbent (default 0.01)

    Returns:
        Updated list of (symbol, momentum_score) with buffer rule applied.
    """
    if len(ranked_candidates) <= target_count:
        return list(ranked_candidates)

    result = list(ranked_candidates)
    cutoff_idx = target_count - 1  # 0-indexed: index 2 is rank #3
    next_idx = target_count        # 0-indexed: index 3 is rank #4

    cutoff_candidate = result[cutoff_idx]
    next_candidate = result[next_idx]

    cutoff_sym, cutoff_score = cutoff_candidate
    next_sym, next_score = next_candidate

    # If the incumbent is at next_idx (#4), and cutoff (#3) is NOT currently held:
    # check if the score difference is less than the threshold
    if next_sym in incumbent_holdings and cutoff_sym not in incumbent_holdings:
        score_diff = cutoff_score - next_score
        if score_diff < threshold:
            # Swap positions: incumbent keeps the cutoff rank
            result[cutoff_idx], result[next_idx] = result[next_idx], result[cutoff_idx]

    return result


# ---------------------------------------------------------------------------
# Dynamic Weight Allocation
# ---------------------------------------------------------------------------

def compute_target_weights(
    regime: RegimeState,
    ranked_etfs: list[str],
    cfg: DualMomentumConfig,
) -> dict[str, float]:
    """Compute exact target weights based on regime state and ranked candidate ETFs.

    Normal Bull Mode (VIX < 22):
      Top 1: 35%, Top 2: 35%, Top 3: 20%, Cash: 10%
    Defensive De-Risking (22 <= VIX < 32):
      Top 1: 25%, Top 2: 25%, Cash: 50%
    Preservation / Risk-Off (VIX >= 32 or Bearish Trend):
      Cash: 100%
    """
    weights: dict[str, float] = {sym: 0.0 for sym in cfg.satellite_symbols}
    cash_sym = cfg.safe_harbor_symbol
    weights[cash_sym] = 0.0

    if regime == RegimeState.RISK_OFF:
        weights[cash_sym] = 1.0
        return weights

    if regime == RegimeState.RISK_ON_DEFENSIVE:
        weights[cash_sym] = cfg.weight_defensive_cash
        if len(ranked_etfs) >= 1:
            weights[ranked_etfs[0]] = cfg.weight_defensive_top1
        if len(ranked_etfs) >= 2:
            weights[ranked_etfs[1]] = cfg.weight_defensive_top2
        return weights

    # Normal Bull Mode
    weights[cash_sym] = cfg.weight_normal_cash
    if len(ranked_etfs) >= 1:
        weights[ranked_etfs[0]] = cfg.weight_normal_top1
    if len(ranked_etfs) >= 2:
        weights[ranked_etfs[1]] = cfg.weight_normal_top2
    if len(ranked_etfs) >= 3:
        weights[ranked_etfs[2]] = cfg.weight_normal_top3

    return weights


# ---------------------------------------------------------------------------
# Stateful Strategy Engine
# ---------------------------------------------------------------------------

@dataclass
class StepEvaluationResult:
    date: date
    regime: RegimeState
    rebalance_triggered: bool
    trigger_type: RebalanceTrigger
    target_weights: dict[str, float]
    ranked_etfs: list[tuple[str, float]]
    consecutive_below_sma: int
    consecutive_above_sma: int
    benchmark_close: float
    benchmark_sma: float
    vix: float
    reason: str


class DualMomentumEngine:
    """State machine governing Dual-Momentum Tactical ETF Rotation.

    Tracks:
    - 200-day SMA consecutive close counts
    - Re-entry hysteresis (3 consecutive closes above 200d SMA AND VIX < 25)
    - Weekly Friday circuit breaker checks
    - Trimester rebalance calendar
    - Turnover buffer rule on incumbent holdings
    """

    def __init__(self, cfg: Optional[DualMomentumConfig] = None):
        self.cfg = cfg or DualMomentumConfig()
        self.regime: RegimeState = RegimeState.RISK_OFF
        self.consecutive_below_sma: int = 0
        self.consecutive_above_sma: int = 0
        self.current_holdings: set[str] = set()
        self.last_rebalance_date: Optional[date] = None

    def reset(self, initial_regime: RegimeState = RegimeState.RISK_OFF) -> None:
        self.regime = initial_regime
        self.consecutive_below_sma = 0
        self.consecutive_above_sma = 0
        self.current_holdings = set()
        self.last_rebalance_date = None

    def update_daily_streaks(self, benchmark_close: float, benchmark_sma: float) -> None:
        """Update consecutive closes above and below 200-day SMA."""
        if np.isnan(benchmark_sma) or benchmark_sma <= 0:
            return

        if benchmark_close > benchmark_sma:
            self.consecutive_above_sma += 1
            self.consecutive_below_sma = 0
        else:
            self.consecutive_below_sma += 1
            self.consecutive_above_sma = 0

    def rank_satellite_universe(
        self,
        prices_df: pd.DataFrame,
        target_count: int,
    ) -> list[tuple[str, float]]:
        """Compute momentum scores and apply turnover buffer against current holdings."""
        scores_series = compute_relative_momentum_scores(
            prices_df[self.cfg.satellite_symbols],
            lookback_12w=self.cfg.lookback_12w,
            lookback_24w=self.cfg.lookback_24w,
            w1=self.cfg.weight_12w,
            w2=self.cfg.weight_24w,
        )
        ranked = list(scores_series.items())
        buffered = apply_turnover_buffer(
            ranked,
            incumbent_holdings=self.current_holdings,
            target_count=target_count,
            threshold=self.cfg.turnover_buffer_threshold,
        )
        return buffered

    def evaluate_step(
        self,
        eval_date: date,
        prices_history: pd.DataFrame,
        benchmark_close: float,
        benchmark_sma: float,
        vix_value: float,
        is_quarter_rebalance: Optional[bool] = None,
        is_friday_check: Optional[bool] = None,
    ) -> StepEvaluationResult:
        """Run daily evaluation step.

        Parameters:
            eval_date: current trading date
            prices_history: DataFrame of close prices for candidate ETFs (at least 120 bars)
            benchmark_close: latest close of S&P 500
            benchmark_sma: latest 200-day SMA of S&P 500
            vix_value: latest close of CBOE VIX
            is_quarter_rebalance: override/detect trimester rebalance
            is_friday_check: override/detect Friday weekly risk check
        """
        # 1. Update streak counts
        self.update_daily_streaks(benchmark_close, benchmark_sma)

        if is_quarter_rebalance is None:
            is_quarter_rebalance = is_trimester_rebalance_date(eval_date, self.cfg.rebalance_months)
        if is_friday_check is None:
            is_friday_check = is_friday(eval_date)

        rebalance_triggered = False
        trigger_type = RebalanceTrigger.NONE
        reason = ""

        # Check circuit breaker conditions
        # Rule 1: S&P 500 closed below 200-day SMA for 2 consecutive sessions
        # Rule 2: VIX spiked to or above 32
        circuit_breaker_active = (
            self.consecutive_below_sma >= self.cfg.consecutive_below_sma_exit
            or vix_value >= self.cfg.vix_crisis_threshold
        )

        # -------------------------------------------------------------------
        # Regime State Machine & Hysteresis
        # -------------------------------------------------------------------
        if self.regime == RegimeState.RISK_OFF:
            # Re-Entry Hysteresis Rule:
            # Require S&P 500 to close above 200-day SMA for 3 consecutive trading days
            # AND VIX to be below 25 before re-entering equities.
            can_reenter = (
                self.consecutive_above_sma >= self.cfg.consecutive_above_sma_reenter
                and vix_value < self.cfg.vix_reentry_threshold
            )
            if can_reenter:
                if vix_value < self.cfg.vix_defensive_threshold:
                    self.regime = RegimeState.RISK_ON_NORMAL
                else:
                    self.regime = RegimeState.RISK_ON_DEFENSIVE

                rebalance_triggered = True
                trigger_type = RebalanceTrigger.HYSTERESIS_REENTRY
                reason = (
                    f"Re-entry hysteresis satisfied: S&P > 200 SMA for "
                    f"{self.consecutive_above_sma} days, VIX={vix_value:.2f} < {self.cfg.vix_reentry_threshold}"
                )
            else:
                reason = "Holding Risk-Off liquidity; waiting for re-entry hysteresis conditions."

        else:
            # Currently Risk-On (Normal or Defensive)
            # Check weekly circuit breaker on Fridays (or daily if circuit breaker active)
            if is_friday_check and circuit_breaker_active:
                self.regime = RegimeState.RISK_OFF
                rebalance_triggered = True
                trigger_type = RebalanceTrigger.CIRCUIT_BREAKER
                cb_reasons = []
                if self.consecutive_below_sma >= self.cfg.consecutive_below_sma_exit:
                    cb_reasons.append(f"S&P 500 below 200d SMA for {self.consecutive_below_sma} consecutive days")
                if vix_value >= self.cfg.vix_crisis_threshold:
                    cb_reasons.append(f"VIX={vix_value:.2f} >= {self.cfg.vix_crisis_threshold}")
                reason = "Weekly Risk-Off Circuit Breaker triggered: " + " and ".join(cb_reasons)

            elif is_quarter_rebalance:
                # Offensive Trimester Rebalance
                # Check VIX regime
                if vix_value < self.cfg.vix_defensive_threshold:
                    self.regime = RegimeState.RISK_ON_NORMAL
                elif vix_value < self.cfg.vix_crisis_threshold:
                    self.regime = RegimeState.RISK_ON_DEFENSIVE
                else:
                    self.regime = RegimeState.RISK_OFF

                rebalance_triggered = True
                trigger_type = RebalanceTrigger.TRIMESTER
                reason = f"Trimester rebalance date ({eval_date}); VIX={vix_value:.2f}"

            elif is_friday_check:
                # Friday Volatility adjustment (between Normal and Defensive)
                if self.regime == RegimeState.RISK_ON_NORMAL and vix_value >= self.cfg.vix_defensive_threshold:
                    self.regime = RegimeState.RISK_ON_DEFENSIVE
                    rebalance_triggered = True
                    trigger_type = RebalanceTrigger.VOLATILITY_DERISK
                    reason = f"Volatility de-risking: VIX risen to {vix_value:.2f} >= {self.cfg.vix_defensive_threshold}"
                elif self.regime == RegimeState.RISK_ON_DEFENSIVE and vix_value < self.cfg.vix_defensive_threshold:
                    self.regime = RegimeState.RISK_ON_NORMAL
                    rebalance_triggered = True
                    trigger_type = RebalanceTrigger.VOLATILITY_RERISK
                    reason = f"Volatility re-risking: VIX subsided to {vix_value:.2f} < {self.cfg.vix_defensive_threshold}"
                else:
                    reason = f"Weekly risk check passed; regime remains {self.regime.value}"

        # -------------------------------------------------------------------
        # Target Weights Calculation
        # -------------------------------------------------------------------
        target_count = 3 if self.regime == RegimeState.RISK_ON_NORMAL else 2
        ranked_etfs = self.rank_satellite_universe(prices_history, target_count=target_count)

        if rebalance_triggered:
            target_symbols = [sym for sym, _ in ranked_etfs]
            target_weights = compute_target_weights(self.regime, target_symbols, self.cfg)
            # Update tracked holdings
            self.current_holdings = {
                sym for sym, wt in target_weights.items()
                if wt > 0 and sym != self.cfg.safe_harbor_symbol
            }
            self.last_rebalance_date = eval_date
        else:
            # Maintain previous weights if no rebalance triggered
            target_symbols = [sym for sym, _ in ranked_etfs]
            target_weights = compute_target_weights(self.regime, target_symbols, self.cfg)

        return StepEvaluationResult(
            date=eval_date,
            regime=self.regime,
            rebalance_triggered=rebalance_triggered,
            trigger_type=trigger_type,
            target_weights=target_weights,
            ranked_etfs=ranked_etfs,
            consecutive_below_sma=self.consecutive_below_sma,
            consecutive_above_sma=self.consecutive_above_sma,
            benchmark_close=benchmark_close,
            benchmark_sma=benchmark_sma,
            vix=vix_value,
            reason=reason,
        )
