"""WFO entry point. Run: python -m scripts.run_wfo --config config/wfo.yaml"""
from __future__ import annotations
import argparse
import hashlib
import json
import logging
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import yaml

from backtest.wfo.grid import expand_grid
from backtest.wfo.report import (
    GateConfig, aggregate_results, emit_live_overrides, emit_summary_md,
)
from backtest.wfo.runner import WFORunner
from backtest.wfo.universe import scan_alpaca_universe
from broker.alpaca_client import AlpacaClient
from broker.alpaca_data import AlpacaData
from core.asset_class import AssetClassConfig

logger = logging.getLogger("wfo")


def _git_sha() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], text=True
        ).strip()
    except Exception:
        return "unknown"


def _run_id(merged_cfg: dict) -> str:
    payload = json.dumps(merged_cfg, sort_keys=True, default=str)
    digest = hashlib.blake2b(payload.encode("utf-8"), digest_size=4).hexdigest()
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M")
    return f"{ts}_{digest}"


def _build_asset_class_configs(settings_cfg: dict, wfo_cfg: dict
                               ) -> dict[str, AssetClassConfig]:
    out: dict[str, AssetClassConfig] = {}
    timeframes = wfo_cfg["timeframes"]
    bar_timeframe = timeframes[0]      # finest is fine here; per-task overrides
    for name, raw in settings_cfg["asset_classes"].items():
        out[name] = AssetClassConfig(
            name=name,
            timezone=raw["timezone"],
            session_open_local=raw["session_open_local"],
            session_close_local=raw["session_close_local"],
            opening_blackout_min=settings_cfg["filters"]["opening_blackout_min"],
            bar_timeframe=bar_timeframe,
            slippage_bps=raw.get("slippage_bps", 0.0),
            commission_per_share=raw.get("commission_per_share", 0.0),
            commission_bps=raw.get("commission_bps", 0.0),
        )
    return out


def _resolve_universe(wfo_cfg: dict, client: AlpacaClient) -> list[tuple[str, str]]:
    src = wfo_cfg["universe"]["source"]
    if src == "symbols":
        return [(s, "us_equity" if "/" not in s else "crypto")
                for s in wfo_cfg["universe"]["symbols"]]
    if src == "alpaca_scan":
        scan = wfo_cfg["universe"]["alpaca_scan"]
        return scan_alpaca_universe(
            client,
            classes=scan["classes"],
            min_dollar_volume_20d=scan["min_dollar_volume_20d"],
            top_n_per_class=scan["top_n_per_class"],
            cache_dir=scan["cache_dir"],
        )
    raise ValueError(f"Unknown universe.source: {src!r}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Walk-Forward Optimization runner")
    parser.add_argument("--config", default="config/wfo.yaml",
                        help="WFO meta-config")
    parser.add_argument("--settings", default="config/settings.yaml",
                        help="Live settings (asset class definitions etc.)")
    parser.add_argument("--run-id", default=None,
                        help="Override deterministic run id (forces a fresh dir)")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    wfo_cfg = yaml.safe_load(Path(args.config).read_text())
    settings_cfg = yaml.safe_load(Path(args.settings).read_text())

    run_id = args.run_id or _run_id({"wfo": wfo_cfg, "settings": settings_cfg})
    output_dir = Path(wfo_cfg["run"]["output_root"]) / run_id
    output_dir.mkdir(parents=True, exist_ok=True)

    manifest_path = output_dir / "manifest.json"
    manifest = {
        "run_id": run_id,
        "git_sha": _git_sha(),
        "started_at": datetime.now(timezone.utc).isoformat(),
        "wfo_config_path": args.config,
        "settings_config_path": args.settings,
    }
    manifest_path.write_text(json.dumps(manifest, indent=2))
    logger.info("WFO_BOOT run_id=%s output_dir=%s", run_id, output_dir)

    client = AlpacaClient()
    data = AlpacaData(client, cache_dir=settings_cfg.get("backtest", {}
                                          ).get("cache_dir", "runtime/bars_cache"))
    universe = _resolve_universe(wfo_cfg, client)
    logger.info("WFO_UNIVERSE size=%d", len(universe))

    ac_configs = _build_asset_class_configs(settings_cfg, wfo_cfg)
    history = wfo_cfg["history"]

    def bars_loader(symbol: str, asset_class: str, timeframe: str):
        start = datetime.fromisoformat(str(history["start"])).replace(tzinfo=timezone.utc)
        end = datetime.fromisoformat(str(history["end"])).replace(tzinfo=timezone.utc)
        return data.get_bars(symbol, asset_class, timeframe,
                             start=start, end=end, use_cache=True)

    runner_cfg = {
        "run": wfo_cfg["run"],
        "history": history,
        "windowing": wfo_cfg["windowing"],
        "timeframes": wfo_cfg["timeframes"],
        "fitness": wfo_cfg["fitness"],
        "gate": wfo_cfg["gate"],
        "grid": wfo_cfg["grid"],
        "position_management": wfo_cfg["position_management"],
        "risk": settings_cfg["risk"],
        "filters": settings_cfg["filters"],
    }

    runner = WFORunner(
        cfg=runner_cfg, asset_class_configs=ac_configs,
        symbols=universe, bars_loader=bars_loader, output_dir=output_dir,
    )
    parquet_path = runner.run()

    # Aggregate + emit
    df = pd.read_parquet(parquet_path)
    last_walk_combos = _build_last_walk_combos_index(df, runner_cfg)
    aggregated = aggregate_results(
        df, GateConfig(wfe_min=wfo_cfg["gate"]["wfe_min"],
                       require_positive_oos_pnl=wfo_cfg["gate"]["require_positive_oos_pnl"]),
    )
    emit_live_overrides(aggregated, last_walk_combos,
                        output_dir / "live_overrides.yaml",
                        run_id=run_id, git_sha=manifest["git_sha"],
                        gate=GateConfig(**wfo_cfg["gate"]))
    emit_summary_md(aggregated, output_dir / "summary.md",
                    run_id=run_id, git_sha=manifest["git_sha"],
                    gate=GateConfig(**wfo_cfg["gate"]))

    manifest["completed_at"] = datetime.now(timezone.utc).isoformat()
    manifest["evaluated_groups"] = int(len(aggregated))
    manifest["passed_groups"] = int(aggregated["passed"].sum() if not aggregated.empty else 0)
    manifest_path.write_text(json.dumps(manifest, indent=2))
    logger.info("WFO_DONE run_id=%s passed=%d / %d",
                run_id, manifest["passed_groups"], manifest["evaluated_groups"])
    return 0


def _build_last_walk_combos_index(df: pd.DataFrame, runner_cfg: dict
                                  ) -> dict[str, tuple[dict, dict]]:
    """For every fingerprint in df, look up its (setup_values, pm_values).

    Cheap: rebuild the grid once and index by fingerprint.
    """
    combos = expand_grid(runner_cfg["grid"], runner_cfg["position_management"])
    return {c.fingerprint: (c.setup_values, c.pm_values) for c in combos}


if __name__ == "__main__":
    sys.exit(main())
