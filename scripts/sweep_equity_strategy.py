#!/usr/bin/env python3
"""Universal grid-sweep harness for equity strategies on the Russell 1000.

For a chosen strategy:

  1. Load its production YAML (config/settings_<strategy>_equity.yaml).
  2. Replace the symbols list with the universe (CSV).
  3. Read cached bars per symbol at the strategy's bar_timeframe.
  4. Per (symbol × param-combo):
       - patch the config with the combo's setup values
       - split bars into IS (first 70%) / OOS (last 30%)
       - run IntradayReplay separately on IS and OOS
       - compute per-trade Sharpe + trade count + total PnL on each side
  5. Pick the combo with the best aggregate OOS Sharpe across all symbols
     (subject to min trade count).
  6. With the chosen combo, list the symbols whose OOS Sharpe >= threshold
     and OOS trades >= floor — these are the proposed universe.
  7. Write results.parquet + report.md under runtime/wfo/<run_dir>/.

Designed so each strategy gets its own --grid-spec JSON (small) so the harness
stays generic. The grid is a flat dict {param_path: [values]}; param_path is
'setups.<setup_name>.<key>' to patch a setup field, or
'position_management.<key>' to patch position-management.

Usage:
    python scripts/sweep_equity_strategy.py \
        --strategy rsi --setup rsi_reversion \
        --universe config/universe_russell_1000.csv \
        --grid-spec scripts/grids/rsi.json \
        --output-dir runtime/wfo/pilot_rsi \
        [--limit-symbols 50]   # debug helper
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
import sys
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from itertools import product
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from backtest.intraday_replay import IntradayReplay
from broker.alpaca_data import AlpacaData
from core.bar import Bar
from main import build_asset_class_configs, load_config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("sweep")
# silence chatty submodules during the sweep
logging.getLogger("backtest.intraday_replay").setLevel(logging.WARNING)
logging.getLogger("intraday_replay").setLevel(logging.WARNING)


@dataclass(frozen=True)
class Combo:
    values: tuple  # (("setups.rsi_reversion.threshold", 30), ...)

    @property
    def label(self) -> str:
        return ", ".join(f"{k.split('.')[-1]}={v}" for k, v in self.values)

    def to_dict(self) -> dict:
        return {k: v for k, v in self.values}


def _expand_grid(spec: dict[str, list]) -> list[Combo]:
    keys = sorted(spec.keys())
    if not keys:
        return [Combo(values=())]
    value_lists = [spec[k] for k in keys]
    return [
        Combo(values=tuple(zip(keys, combo)))
        for combo in product(*value_lists)
    ]


def _patch_cfg(base_cfg: dict, combo: Combo) -> dict:
    cfg = deepcopy(base_cfg)
    for path, value in combo.values:
        parts = path.split(".")
        cur = cfg
        for p in parts[:-1]:
            cur = cur.setdefault(p, {})
        cur[parts[-1]] = value
    return cfg


def _load_universe(path: Path) -> list[str]:
    syms: list[str] = []
    import csv
    with path.open() as f:
        reader = csv.reader(f)
        header = next(reader, None)
        if header and header[0].strip().lower() != "symbol":
            syms.append(header[0].strip().upper())
        for row in reader:
            if row and row[0].strip():
                syms.append(row[0].strip().upper())
    return sorted(set(syms))


def _read_cached_bars(symbol: str, timeframe: str,
                      start: datetime, end: datetime,
                      cache_dir: Path) -> list[Bar] | None:
    """Read bars from the AlpacaData parquet cache directly (no client calls)."""
    safe = symbol.replace("/", "-")
    path = cache_dir / f"{safe}_{timeframe}_{start.isoformat()}_{end.isoformat()}.parquet"
    if not path.exists():
        return None
    try:
        df = pd.read_parquet(path)
    except Exception as exc:
        log.warning("CACHE_READ_FAILED sym=%s err=%s", symbol, exc)
        return None
    return [
        Bar(symbol=symbol, ts=row.ts.to_pydatetime() if hasattr(row.ts, "to_pydatetime") else row.ts,
            open=float(row.open), high=float(row.high), low=float(row.low),
            close=float(row.close), volume=float(row.volume))
        for row in df.itertuples(index=False)
    ]


def _split_is_oos(bars: list[Bar], is_frac: float = 0.7) -> tuple[list[Bar], list[Bar]]:
    if not bars:
        return [], []
    cut = int(len(bars) * is_frac)
    return bars[:cut], bars[cut:]


def _trade_sharpe(trades: pd.DataFrame) -> float:
    """Per-trade Sharpe (mean R / std R). NOT annualized — used for cross-combo
    ranking only, so we don't need a frequency factor.
    """
    if trades.empty or "R_realized" not in trades.columns:
        return 0.0
    R = trades["R_realized"].astype(float)
    if len(R) < 2 or R.std() == 0:
        return 0.0
    return float(R.mean() / R.std())


def _replay_on_bars(cfg: dict, symbol: str, asset_class: str,
                    bars: list[Bar]) -> dict:
    """Run IntradayReplay on the given bars for a single symbol; return summary."""
    if len(bars) < 50:  # need enough warmup
        return {"trades": 0, "pnl_usd": 0.0, "sharpe": 0.0,
                "win_rate": 0.0, "avg_R": 0.0}
    asset_classes = build_asset_class_configs(cfg)
    replay = IntradayReplay(
        symbols=[(symbol, asset_class)],
        asset_class_configs=asset_classes,
        bars={symbol: bars},
        initial_equity=cfg["backtest"]["initial_equity"],
        config=cfg,
    )
    result = replay.run()
    trades = result.trades
    return {
        "trades": int(len(trades)),
        "pnl_usd": float(trades["pnl_usd"].sum()) if not trades.empty else 0.0,
        "sharpe": _trade_sharpe(trades),
        "win_rate": float((trades["pnl_usd"] > 0).mean()) if not trades.empty else 0.0,
        "avg_R": (float(trades["R_realized"].mean())
                  if not trades.empty and "R_realized" in trades.columns else 0.0),
    }


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--strategy", required=True,
                   help="Strategy short name; loads config/settings_<n>_equity.yaml")
    p.add_argument("--setup", required=True,
                   help="Setup name in cfg.setups (used for grid path prefix).")
    p.add_argument("--universe", required=True, type=Path)
    p.add_argument("--grid-spec", required=True, type=Path,
                   help="JSON file: {setup_param: [values], ...}. Param paths "
                        "are relative to setups.<setup>.* unless they contain "
                        "a dot, in which case they are absolute config paths.")
    p.add_argument("--output-dir", required=True, type=Path)
    p.add_argument("--start", default=None,
                   help="Override backtest.start in the cfg (else use the cfg value).")
    p.add_argument("--end", default=None)
    p.add_argument("--asset-class", default="equity")
    p.add_argument("--limit-symbols", type=int, default=None)
    p.add_argument("--min-oos-trades", type=int, default=10)
    p.add_argument("--min-oos-sharpe", type=float, default=0.3)
    p.add_argument("--min-aggregate-trades", type=int, default=200)
    p.add_argument("--max-symbols-kept", type=int, default=50)
    args = p.parse_args()

    args.output_dir.mkdir(parents=True, exist_ok=True)

    cfg_path = f"config/settings_{args.strategy}_equity.yaml"
    base_cfg = load_config(cfg_path)
    base_cfg.setdefault("overrides", {})["enabled"] = False  # disable WFO overrides
    timeframe = base_cfg["scheduler"]["bar_timeframe"]
    start = datetime.fromisoformat(args.start or base_cfg["backtest"]["start"]).replace(tzinfo=timezone.utc)
    end = datetime.fromisoformat(args.end or base_cfg["backtest"]["end"]).replace(tzinfo=timezone.utc)
    cache_dir = Path(base_cfg["backtest"].get("cache_dir", "runtime/bars_cache"))

    syms = _load_universe(args.universe)
    if args.limit_symbols:
        syms = syms[: args.limit_symbols]
    log.info("UNIVERSE %d symbols, timeframe=%s", len(syms), timeframe)

    raw_grid: dict = json.loads(args.grid_spec.read_text())
    # Allow both relative and absolute param paths.
    grid_spec: dict[str, list] = {}
    for k, vs in raw_grid.items():
        path = k if "." in k else f"setups.{args.setup}.{k}"
        grid_spec[path] = list(vs)
    combos = _expand_grid(grid_spec)
    log.info("GRID %d combos: %s", len(combos), grid_spec)

    # Stream one symbol at a time to keep peak memory bounded by a single
    # symbol's bar list. With 5Min bars × 32 months × 448 symbols, holding
    # everything in RAM at once is ~5 GB — this loop layout keeps it under
    # ~50 MB at any moment instead. Per-symbol cost: re-instantiate the
    # combo cfg dicts once before the inner loop so we don't deepcopy per
    # combo for every symbol.
    rows: list[dict] = []
    missing: list[str] = []
    n_total = len(combos) * len(syms)
    n_done = 0
    n_loaded = 0

    for si, sym in enumerate(syms):
        bars = _read_cached_bars(sym, timeframe, start, end, cache_dir)
        if not bars:
            missing.append(sym)
            continue
        n_loaded += 1
        is_bars, oos_bars = _split_is_oos(bars, is_frac=0.7)
        # Free the full bar list immediately — IS+OOS slices are independent
        # views and the original is no longer needed.
        del bars

        for ci, combo in enumerate(combos):
            cfg_for_sym = _patch_cfg(base_cfg, combo)
            cfg_for_sym.pop("_per_symbol_overrides", None)
            cfg_for_sym["asset_classes"]["equity"]["symbols"] = [sym]
            try:
                is_m = _replay_on_bars(cfg_for_sym, sym, args.asset_class, is_bars)
                oos_m = _replay_on_bars(cfg_for_sym, sym, args.asset_class, oos_bars)
            except Exception as exc:
                log.warning("REPLAY_FAILED combo=%s sym=%s err=%s",
                            combo.label, sym, exc)
                continue
            row = {
                "combo_idx": ci,
                "combo_label": combo.label,
                "symbol": sym,
                **{f"is_{k}": v for k, v in is_m.items()},
                **{f"oos_{k}": v for k, v in oos_m.items()},
            }
            for k, v in combo.values:
                row[f"param.{k}"] = v
            rows.append(row)
            n_done += 1
            if n_done % 500 == 0:
                log.info("PROGRESS %d/%d sym=%d/%d (%s) loaded=%d missing=%d",
                         n_done, n_total, si + 1, len(syms), sym,
                         n_loaded, len(missing))
        # Drop slices for this symbol before moving to the next.
        del is_bars, oos_bars
    log.info("BARS_LOADED %d (missing=%d)", n_loaded, len(missing))

    if not rows:
        log.error("No replay rows produced — check the grid and cache.")
        return 1

    df = pd.DataFrame(rows)
    pq.write_table(pa.Table.from_pandas(df), args.output_dir / "results.parquet")
    log.info("RESULTS_WRITTEN %s rows=%d", args.output_dir / "results.parquet", len(df))

    # ── Aggregate per-combo and pick global winner ──────────────────────
    agg = (df.groupby(["combo_idx", "combo_label"], as_index=False)
             .agg(symbols=("symbol", "nunique"),
                  total_oos_trades=("oos_trades", "sum"),
                  total_is_trades=("is_trades", "sum"),
                  median_oos_sharpe=("oos_sharpe", "median"),
                  mean_oos_sharpe=("oos_sharpe", "mean"),
                  total_oos_pnl=("oos_pnl_usd", "sum"),
                  symbols_above_floor=("oos_sharpe",
                                        lambda s: int((s >= args.min_oos_sharpe).sum())),
                  ))
    agg = agg[agg["total_oos_trades"] >= args.min_aggregate_trades].copy()
    if agg.empty:
        log.warning("No combo cleared min_aggregate_trades=%d. Falling back to all.",
                    args.min_aggregate_trades)
        agg = (df.groupby(["combo_idx", "combo_label"], as_index=False)
                 .agg(symbols=("symbol", "nunique"),
                      total_oos_trades=("oos_trades", "sum"),
                      median_oos_sharpe=("oos_sharpe", "median"),
                      mean_oos_sharpe=("oos_sharpe", "mean"),
                      total_oos_pnl=("oos_pnl_usd", "sum"),
                      symbols_above_floor=("oos_sharpe",
                                            lambda s: int((s >= args.min_oos_sharpe).sum())),
                      total_is_trades=("is_trades", "sum"),
                      ))
    agg = agg.sort_values(
        ["symbols_above_floor", "median_oos_sharpe", "total_oos_pnl"],
        ascending=False,
    )
    best = agg.iloc[0].to_dict()
    log.info("BEST_COMBO %s", best)

    # ── Per-symbol ranking with the chosen combo ────────────────────────
    per_sym = df[df["combo_idx"] == best["combo_idx"]].copy()
    kept = per_sym[(per_sym["oos_trades"] >= args.min_oos_trades)
                   & (per_sym["oos_sharpe"] >= args.min_oos_sharpe)]
    kept = kept.sort_values("oos_sharpe", ascending=False).head(args.max_symbols_kept)

    # ── Markdown report ─────────────────────────────────────────────────
    report = []
    report.append(f"# {args.strategy}_equity sweep — {datetime.now(timezone.utc).isoformat()}\n")
    report.append(f"- Universe: `{args.universe}` ({n_loaded} symbols loaded, "
                  f"{len(missing)} missing from cache)")
    report.append(f"- Timeframe: `{timeframe}`  Window: `{start.date()} → {end.date()}`")
    report.append(f"- IS/OOS split: 70/30 by bar count")
    report.append(f"- Grid: {len(combos)} combos over {sorted(grid_spec.keys())}\n")

    report.append("## Best global combo (selected)\n")
    report.append(f"**`{best['combo_label']}`**\n")
    report.append("| metric | value |")
    report.append("|---|---|")
    for k in ("symbols_above_floor", "symbols", "total_oos_trades",
              "total_is_trades", "median_oos_sharpe",
              "mean_oos_sharpe", "total_oos_pnl"):
        v = best.get(k, float("nan"))
        report.append(f"| {k} | {v:.4f} |" if isinstance(v, float)
                      else f"| {k} | {v} |")

    chosen_combo_dict = combos[int(best["combo_idx"])].to_dict()
    report.append("\n### Chosen parameter values\n```json")
    report.append(json.dumps(chosen_combo_dict, indent=2))
    report.append("```\n")

    report.append("## Top-15 alternative combos\n")
    report.append("| combo | symbols≥floor | trades_oos | median_oos_sharpe | total_oos_pnl |")
    report.append("|---|---|---|---|---|")
    for _, r in agg.head(15).iterrows():
        report.append(f"| {r['combo_label']} | {int(r['symbols_above_floor'])} | "
                      f"{int(r['total_oos_trades'])} | {r['median_oos_sharpe']:.3f} | "
                      f"{r['total_oos_pnl']:.0f} |")

    report.append(f"\n## Symbols surviving filter ({len(kept)} kept; floor "
                  f"oos_sharpe≥{args.min_oos_sharpe} AND oos_trades≥"
                  f"{args.min_oos_trades})\n")
    if kept.empty:
        report.append("**None.** Strategy did not surface a clean universe at this "
                      "Sharpe floor.\n")
    else:
        report.append("| symbol | oos_sharpe | oos_trades | oos_pnl | oos_win_rate | "
                      "is_sharpe | is_trades |")
        report.append("|---|---|---|---|---|---|---|")
        for _, r in kept.iterrows():
            report.append(f"| {r['symbol']} | {r['oos_sharpe']:.3f} | "
                          f"{int(r['oos_trades'])} | {r['oos_pnl_usd']:.0f} | "
                          f"{r['oos_win_rate']:.2%} | {r['is_sharpe']:.3f} | "
                          f"{int(r['is_trades'])} |")

    report.append("\n## IS-vs-OOS distribution (selected combo)\n")
    report.append("| stat | IS sharpe | OOS sharpe | IS trades | OOS trades |")
    report.append("|---|---|---|---|---|")
    for stat in ("mean", "median", "std"):
        f = getattr(per_sym, stat)
        try:
            is_s, oos_s = f(numeric_only=True)["is_sharpe"], f(numeric_only=True)["oos_sharpe"]
            is_t, oos_t = f(numeric_only=True)["is_trades"], f(numeric_only=True)["oos_trades"]
        except Exception:
            continue
        report.append(f"| {stat} | {is_s:.3f} | {oos_s:.3f} | {is_t:.1f} | {oos_t:.1f} |")

    report.append("\n## Proposed YAML diff (preview)\n")
    report.append("Apply to `config/settings_" + args.strategy + "_equity.yaml`:\n")
    report.append("```yaml")
    report.append(f"setups:")
    for k, v in chosen_combo_dict.items():
        if k.startswith(f"setups.{args.setup}."):
            report.append(f"  {args.setup}:")
            report.append(f"    {k.split('.')[-1]}: {v}")
    if any(k.startswith("position_management.") for k in chosen_combo_dict):
        report.append("position_management:")
        for k, v in chosen_combo_dict.items():
            if k.startswith("position_management."):
                report.append(f"  {k.split('.')[-1]}: {v}")
    if not kept.empty:
        report.append("asset_classes:")
        report.append("  equity:")
        report.append("    symbols:")
        for s in kept["symbol"].tolist():
            report.append(f"      - {s}")
    report.append("```\n")

    if missing:
        report.append(f"\n## Missing from cache ({len(missing)} symbols, "
                      "first 30)\n")
        report.append(", ".join(missing[:30]))

    (args.output_dir / "report.md").write_text("\n".join(report))
    log.info("REPORT_WRITTEN %s", args.output_dir / "report.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
