"""WFO orchestrator — outer-loop runner and per-task `_run_one`."""
from __future__ import annotations
import logging
import math
from copy import deepcopy
from dataclasses import dataclass, field

from backtest.fill_engine import SimulatedFillEngine     # noqa: F401  (used by IntradayReplay)
from backtest.intraday_replay import IntradayReplay
from backtest.performance import compute_metrics
from backtest.wfo.fitness import score
from backtest.wfo.grid import ParamCombo
from backtest.wfo.windowing import Walk
from core.asset_class import AssetClassConfig
from core.bar import Bar

logger = logging.getLogger(__name__)


RUNNER_RESULT_COLUMNS = (
    "symbol", "asset_class", "timeframe", "walk_idx",
    "setup", "fingerprint", "combo_values_json",
    "is_sharpe", "is_trades", "is_pnl", "is_score",
    "oos_sharpe", "oos_trades", "oos_pnl", "oos_max_dd", "oos_avg_R",
    "status", "error",
)


@dataclass
class RunTask:
    symbol: str
    asset_class: str
    timeframe: str
    walk: Walk
    is_bars: list[Bar]
    oos_bars: list[Bar]
    combo: ParamCombo
    initial_equity: float
    ac_configs: dict[str, AssetClassConfig]
    risk_cfg: dict
    filters_cfg: dict
    min_trades: int


def _build_replay_cfg(task: RunTask) -> dict:
    """Synthesize a one-symbol IntradayReplay config from the task.

    All setups except the combo's are forced disabled; the combo's setup
    receives its setup_values; position_management receives pm_values.
    """
    setups: dict = {}
    for name in ("price_discovery", "fade_extreme", "return_to_value", "vwap_bounce"):
        if name == task.combo.setup:
            base = {"enabled": True, "atr_mult_stop": 1.0, "cooldown_bars": 12}
            base.update(task.combo.setup_values)
            # Carry through commonly-required keys that some setups read with .get()
            base.setdefault("target_R", 1.5)
            base.setdefault("arm_window_bars", 6)
            base.setdefault("scale_offsets_atr", [0.0, 0.25, 0.5])
            base.setdefault("scale_weights", [0.4, 0.35, 0.25])
            setups[name] = base
        else:
            setups[name] = {
                "enabled": False, "atr_mult_stop": 1.0, "target_R": 1.5,
                "arm_window_bars": 6, "cooldown_bars": 12,
                "scale_offsets_atr": [0.0], "scale_weights": [1.0],
            }
    return {
        "setups": setups,
        "risk": deepcopy(task.risk_cfg),
        "filters": deepcopy(task.filters_cfg),
        "position_management": deepcopy(task.combo.pm_values),
        "scheduler": {"bar_timeframe": task.timeframe},
    }


def _empty_metric_row(task: RunTask, *, status: str, error: str = "") -> dict:
    import json
    return {
        "symbol": task.symbol, "asset_class": task.asset_class,
        "timeframe": task.timeframe, "walk_idx": task.walk.idx,
        "setup": task.combo.setup, "fingerprint": task.combo.fingerprint,
        "combo_values_json": json.dumps({"setup": task.combo.setup_values,
                                         "pm": task.combo.pm_values},
                                        sort_keys=True),
        "is_sharpe": math.nan, "is_trades": 0, "is_pnl": 0.0,
        "is_score": math.nan,
        "oos_sharpe": math.nan, "oos_trades": 0, "oos_pnl": 0.0,
        "oos_max_dd": math.nan, "oos_avg_R": math.nan,
        "status": status, "error": error,
    }


def _run_one(task: RunTask) -> dict:
    """Run one (symbol, timeframe, walk, combo). Never raises."""
    try:
        cfg = _build_replay_cfg(task)
        is_result = IntradayReplay(
            symbols=[(task.symbol, task.asset_class)],
            asset_class_configs=task.ac_configs,
            bars={task.symbol: task.is_bars},
            initial_equity=task.initial_equity,
            config=cfg,
        ).run()
        is_metrics = compute_metrics(is_result.equity_curve, is_result.trades)
        is_score = score(is_metrics, min_trades=task.min_trades)

        if is_score is None:
            row = _empty_metric_row(task, status="below_min_trades")
            row["is_sharpe"] = is_metrics.get("sharpe", math.nan)
            row["is_trades"] = int(is_metrics.get("trades", 0))
            row["is_pnl"] = float(is_result.trades["pnl_usd"].sum()
                                  if not is_result.trades.empty else 0.0)
            return row

        oos_result = IntradayReplay(
            symbols=[(task.symbol, task.asset_class)],
            asset_class_configs=task.ac_configs,
            bars={task.symbol: task.oos_bars},
            initial_equity=task.initial_equity,
            config=cfg,
        ).run()
        oos_metrics = compute_metrics(oos_result.equity_curve, oos_result.trades)

        row = _empty_metric_row(task, status="ok")
        row["is_sharpe"] = is_metrics.get("sharpe", math.nan)
        row["is_trades"] = int(is_metrics.get("trades", 0))
        row["is_pnl"] = float(is_result.trades["pnl_usd"].sum()
                              if not is_result.trades.empty else 0.0)
        row["is_score"] = is_score
        row["oos_sharpe"] = oos_metrics.get("sharpe", math.nan)
        row["oos_trades"] = int(oos_metrics.get("trades", 0))
        row["oos_pnl"] = float(oos_result.trades["pnl_usd"].sum()
                               if not oos_result.trades.empty else 0.0)
        row["oos_max_dd"] = oos_metrics.get("max_drawdown", math.nan)
        row["oos_avg_R"] = oos_metrics.get("avg_R", math.nan)
        return row
    except Exception as exc:                                    # noqa: BLE001
        return _empty_metric_row(task, status="failed", error=repr(exc))
