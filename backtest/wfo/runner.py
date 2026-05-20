"""WFO orchestrator — outer-loop runner and per-task `_run_one`."""
from __future__ import annotations
import logging
import math
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
from joblib import Parallel, delayed

from backtest.fill_engine import SimulatedFillEngine     # noqa: F401  (used by IntradayReplay)
from backtest.intraday_replay import IntradayReplay
from backtest.performance import compute_metrics
from backtest.wfo.fitness import score
from backtest.wfo.grid import ParamCombo, expand_grid
from backtest.wfo.windowing import Walk, make_walks, parse_duration
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


# ---------------------------------------------------------------------------
# WFORunner — orchestration + parquet streaming
# ---------------------------------------------------------------------------

_PARQUET_SCHEMA = pa.schema([
    pa.field("symbol", pa.string()),
    pa.field("asset_class", pa.string()),
    pa.field("timeframe", pa.string()),
    pa.field("walk_idx", pa.int32()),
    pa.field("setup", pa.string()),
    pa.field("fingerprint", pa.string()),
    pa.field("combo_values_json", pa.string()),
    pa.field("is_sharpe", pa.float64()),
    pa.field("is_trades", pa.int32()),
    pa.field("is_pnl", pa.float64()),
    pa.field("is_score", pa.float64()),
    pa.field("oos_sharpe", pa.float64()),
    pa.field("oos_trades", pa.int32()),
    pa.field("oos_pnl", pa.float64()),
    pa.field("oos_max_dd", pa.float64()),
    pa.field("oos_avg_R", pa.float64()),
    pa.field("status", pa.string()),
    pa.field("error", pa.string()),
])


def _slice_bars(bars: list[Bar], start: datetime, end: datetime) -> list[Bar]:
    return [b for b in bars if start <= b.ts < end]


@dataclass
class WFORunner:
    cfg: dict
    asset_class_configs: dict[str, AssetClassConfig]
    symbols: list[tuple[str, str]]
    bars_loader: Callable[[str, str, str], list[Bar]]
    output_dir: Path

    def run(self) -> Path:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        parquet_path = self.output_dir / "results.parquet"

        history = self.cfg["history"]
        start = self._parse_dt(history["start"])
        end = self._parse_dt(history["end"])
        is_dur = parse_duration(self.cfg["windowing"]["in_sample"])
        oos_dur = parse_duration(self.cfg["windowing"]["out_of_sample"])
        step_str = self.cfg["windowing"].get("step")
        step = parse_duration(step_str) if step_str else None
        walks = make_walks(start, end, in_sample=is_dur, out_of_sample=oos_dur, step=step)

        combos = expand_grid(self.cfg["grid"], self.cfg["position_management"])
        timeframes = self.cfg["timeframes"]
        n_jobs = self.cfg["run"]["parallelism"]

        completed = self._load_completed_keys(parquet_path)
        # Open writer in append mode by reading existing rows once and
        # rewriting them as the writer's first batch (ParquetWriter cannot
        # itself open in append mode; this preserves the durability invariant).
        writer = pq.ParquetWriter(parquet_path.with_suffix(".parquet.tmp"),
                                  _PARQUET_SCHEMA)
        try:
            if completed:
                self._copy_existing_rows(parquet_path, writer)
            for symbol, asset_class in self.symbols:
                for timeframe in timeframes:
                    bars = self.bars_loader(symbol, asset_class, timeframe)
                    if not bars:
                        logger.warning("BARS_UNAVAILABLE symbol=%s tf=%s",
                                       symbol, timeframe)
                        continue
                    tasks = self._build_tasks(symbol, asset_class, timeframe,
                                              bars, walks, combos, completed)
                    if not tasks:
                        continue
                    rows = Parallel(n_jobs=n_jobs, backend="loky")(
                        delayed(_run_one)(t) for t in tasks
                    )
                    self._write_rows(writer, rows)
        finally:
            writer.close()
        # Atomic swap of the rewritten file into place
        parquet_path.with_suffix(".parquet.tmp").replace(parquet_path)
        return parquet_path

    @staticmethod
    def _load_completed_keys(parquet_path: Path) -> set[tuple[str, str, int, str]]:
        if not parquet_path.exists():
            return set()
        df = pd.read_parquet(parquet_path,
                             columns=["symbol", "timeframe", "walk_idx", "fingerprint"])
        return set(zip(df["symbol"], df["timeframe"],
                       df["walk_idx"].astype(int), df["fingerprint"]))

    @staticmethod
    def _copy_existing_rows(parquet_path: Path, writer: pq.ParquetWriter) -> None:
        existing = pa.parquet.read_table(parquet_path, schema=_PARQUET_SCHEMA)
        writer.write_table(existing)

    @staticmethod
    def _parse_dt(value) -> datetime:
        if isinstance(value, datetime):
            return value
        from datetime import timezone
        return datetime.fromisoformat(str(value)).replace(tzinfo=timezone.utc)

    def _build_tasks(self, symbol, asset_class, timeframe, bars, walks, combos,
                     completed):
        out: list[RunTask] = []
        for walk in walks:
            is_bars = _slice_bars(bars, walk.is_start, walk.is_end)
            oos_bars = _slice_bars(bars, walk.oos_start, walk.oos_end)
            if not is_bars:
                continue
            for combo in combos:
                key = (symbol, timeframe, walk.idx, combo.fingerprint)
                if key in completed:
                    continue
                out.append(RunTask(
                    symbol=symbol, asset_class=asset_class, timeframe=timeframe,
                    walk=walk, is_bars=is_bars, oos_bars=oos_bars, combo=combo,
                    initial_equity=self.cfg["history"]["initial_equity"],
                    ac_configs=self.asset_class_configs,
                    risk_cfg=self.cfg["risk"],
                    filters_cfg=self.cfg["filters"],
                    min_trades=self.cfg["fitness"]["min_trades"],
                ))
        return out

    @staticmethod
    def _write_rows(writer: pq.ParquetWriter, rows: list[dict]) -> None:
        if not rows:
            return
        table = pa.Table.from_pylist(rows, schema=_PARQUET_SCHEMA)
        writer.write_table(table)
