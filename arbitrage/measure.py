"""Edge-measurement pipeline: load -> align -> gross spread -> net edge sweep -> report."""
import argparse
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

import pandas as pd

from arbitrage.align import align_pair, clock_skew_stats
from arbitrage.config import Config, load_config
from arbitrage.connectors.base import VenueConnector
from arbitrage.connectors.binance_archive import BinanceArchiveConnector
from arbitrage.connectors.recorder_parquet import RecorderParquetConnector
from arbitrage.costs import round_trip_cost_bps
from arbitrage.net_edge import net_bps, net_edge_stats
from arbitrage.report import build_report
from arbitrage.spread import gross_spread, mid_quote


def _days(start: date, end: date):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def measure_pair(
    pair: str,
    config: Config,
    connectors: dict[str, VenueConnector],
    start: date,
    end: date,
) -> dict:
    """Measure net arbitrage edge for ``pair`` between exactly two venues.

    Days are loaded and aligned one at a time; only the per-direction gross
    edge in bps is kept across days. Cost is a constant per (slippage, leg_risk)
    cell and is direction-independent, so the better direction is the one with
    the higher median gross edge, and it is used for the whole sweep.
    """
    if len(connectors) != 2:
        raise ValueError(f"need exactly 2 venues, got {list(connectors)}")
    a, b = list(connectors)
    directions = [(a, b), (b, a)]
    gross_bps: dict[tuple[str, str], list[pd.Series]] = {d: [] for d in directions}
    skews: list[dict[str, float]] = []

    for day in _days(start, end):
        day_start = datetime.combine(day, time.min, tzinfo=timezone.utc)
        day_end = day_start + timedelta(days=1) - timedelta(milliseconds=1)
        quotes = {v: c.load_quotes(pair, day_start, day_end) for v, c in connectors.items()}
        if any(len(q) == 0 for q in quotes.values()):
            continue
        aligned = align_pair(quotes, config.alignment_grid_ms, config.staleness_ms)
        skews.append(clock_skew_stats(quotes))
        for buy, sell in directions:
            gross_bps[(buy, sell)].append(
                net_bps(gross_spread(aligned, buy, sell), mid_quote(aligned, buy), 0.0)
            )

    series = {
        d: (pd.concat(parts) if parts else pd.Series(dtype="float64"))
        for d, parts in gross_bps.items()
    }
    # idxmax-style pick; ties and all-NaN fall back to the first direction
    best = directions[0]
    if series[directions[1]].median() > series[directions[0]].median():
        best = directions[1]
    buy, sell = best

    slippage: dict[float, dict] = {}
    for slip in config.slippage_bps_grid:
        cells = {}
        for lr in config.leg_risk_grid:
            cost = round_trip_cost_bps(
                config, buy, sell, slip, lr, config.adverse_move_bps, config.rebalancing_bps
            )
            st = net_edge_stats(series[best] - cost, config.alignment_grid_ms)
            cells[lr] = {
                "median_bps": st["median_bps"],
                "pct_positive": st["pct_positive"],
                "persistence_median_ms": st["persistence_median_ms"],
            }
        slippage[slip] = {"leg_risk": cells}

    return {
        "venue_buy": buy,
        "venue_sell": sell,
        "n_aligned_rows": int(len(series[best])),
        "clock_skew_ms": (
            {v: float(pd.Series([s[v] for s in skews]).mean()) for v in connectors}
            if skews
            else {}
        ),
        "slippage_bps": slippage,
    }


# Binance UM-futures archive; any other venue is read from recorder output via --data-dir.
CONNECTOR_FACTORIES = {"binance": lambda: BinanceArchiveConnector(market="um")}


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="python -m arbitrage.measure")
    p.add_argument("--pair", required=True, help="e.g. BTC/USDT")
    p.add_argument("--venues", nargs="+", required=True, help="exactly two venue names")
    p.add_argument("--start", required=True, type=date.fromisoformat)
    p.add_argument("--end", required=True, type=date.fromisoformat)
    p.add_argument("--config", default="arbitrage/config.yaml")
    p.add_argument(
        "--data-dir",
        default=None,
        help="recorder output dir (<data-dir>/<venue>/<SYM>.parquet); "
        "if given, all venues are read from it instead of the archive",
    )
    p.add_argument("--out-dir", default="arbitrage/reports")
    args = p.parse_args(argv)

    unknown = (
        [] if args.data_dir else [v for v in args.venues if v not in CONNECTOR_FACTORIES]
    )
    if unknown:
        p.error(f"no connector for {unknown}; available: {sorted(CONNECTOR_FACTORIES)}")
    if len(args.venues) != 2 or len(set(args.venues)) != 2:
        p.error("need exactly two distinct venues to measure a spread")

    config = load_config(args.config)
    if args.data_dir:
        connectors = {v: RecorderParquetConnector(args.data_dir, v) for v in args.venues}
    else:
        connectors = {v: CONNECTOR_FACTORIES[v]() for v in args.venues}
    results = {args.pair: measure_pair(args.pair, config, connectors, args.start, args.end)}
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    stem = args.pair.replace("/", "")
    build_report(results, config, out / f"{stem}.json", out / f"{stem}.md")
    print(f"wrote {out / (stem + '.md')}")


if __name__ == "__main__":
    main()
