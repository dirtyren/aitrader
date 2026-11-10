import json
from pathlib import Path

from arbitrage.config import Config


def go_nogo(
    median_net_bps: float,
    persistence_median_ms: int,
    min_slippage_bps: float,
    min_persistence_ms: int,
) -> str:
    """'real' iff median net edge >= min_slippage_bps and persistence >= min_persistence_ms."""
    if median_net_bps >= min_slippage_bps and persistence_median_ms >= min_persistence_ms:
        return "real"
    return "not real"


def build_report(results: dict, config: Config, json_path: Path, md_path: Path) -> None:
    json_path, md_path = Path(json_path), Path(md_path)
    json_path.write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")

    min_slip = config.go_nogo_min_slippage_bps
    min_pers = config.go_nogo_min_persistence_ms
    lines = [
        "# Arbitrage edge report",
        "",
        f"Go/no-go rule: median net edge >= {min_slip} bps and median persistence "
        f">= {min_pers} ms.",
        "",
        "## Verdicts",
        "",
    ]
    tables: list[str] = []
    for pair, info in results.items():
        tables += [f"## {pair} ({info['venue_buy']} -> {info['venue_sell']})", ""]
        for slip, sdata in info["slippage_bps"].items():
            tables += [
                f"### slippage {slip} bps", "",
                "| leg_risk | median_bps | pct_positive | persistence_median_ms | verdict |",
                "|---|---|---|---|---|",
            ]
            for lr, st in sdata["leg_risk"].items():
                v = go_nogo(st["median_bps"], st["persistence_median_ms"], min_slip, min_pers)
                lines.append(f"- {pair} slippage={slip} leg_risk={lr}: {v}")
                tables.append(
                    f"| {lr} | {st['median_bps']} | {st['pct_positive']} "
                    f"| {st['persistence_median_ms']} | {v} |"
                )
            tables.append("")
    lines.append("")
    md_path.write_text("\n".join(lines + tables) + "\n", encoding="utf-8")
