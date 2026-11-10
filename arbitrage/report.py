import json
from pathlib import Path

from arbitrage.config import Config


def go_nogo(median_net_bps: float, persistence_median_ms: int, min_persistence_ms: int) -> str:
    """'real' iff median net edge > 0 and median persistence > min_persistence_ms (NaN -> not real)."""
    if median_net_bps > 0 and persistence_median_ms > min_persistence_ms:
        return "real"
    return "not real"


def build_report(results: dict, config: Config, json_path: Path, md_path: Path) -> None:
    json_path, md_path = Path(json_path), Path(md_path)
    json_path.write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")

    min_slip = config.go_nogo_min_slippage_bps
    min_pers = config.go_nogo_min_persistence_ms
    headlines = {
        pair: "real" if any(
            go_nogo(st["median_bps"], st["persistence_median_ms"], min_pers) == "real"
            for slip, sdata in info["slippage_bps"].items()
            if float(slip) >= min_slip
            for st in sdata["leg_risk"].values()
        ) else "not real"
        for pair, info in results.items()
    }
    lines = [
        "# Arbitrage edge report",
        "",
        f"Go/no-go rule: median net edge > 0 bps and median persistence > {min_pers} ms, "
        f"judged only on cells with slippage >= {min_slip} bps.",
        "",
        "## Verdicts",
        "",
        *[f"{pair} headline: {v}" for pair, v in headlines.items()],
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
                v = go_nogo(st["median_bps"], st["persistence_median_ms"], min_pers)
                lines.append(f"- {pair} slippage={slip} leg_risk={lr}: {v}")
                tables.append(
                    f"| {lr} | {st['median_bps']} | {st['pct_positive']} "
                    f"| {st['persistence_median_ms']} | {v} |"
                )
            tables.append("")
    lines.append("")
    md_path.write_text("\n".join(lines + tables) + "\n", encoding="utf-8")
