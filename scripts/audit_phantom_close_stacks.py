"""Read-only audit of MySQL/broker position drift across both Alpaca accounts.

Triggered by the 2026-06-02 COIN incident: trader-vwap-wave-equity stacked
22 long broker positions on COIN while MySQL showed 1 short open. This
script aggregates MySQL open rows by (symbol, asset_class) into a signed
qty and compares to the broker's per-symbol aggregated signed qty,
flagging any mismatch.

Usage:
    python -m scripts.audit_phantom_close_stacks

Output: human-readable per-symbol report. Exit code is always 0 — this is
a report, not a CI gate. No mutations.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from typing import Iterable

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from broker.alpaca_router import AlpacaRouter
from state.mysql_store import PositionRow, _build_url


@dataclass
class DriftRow:
    symbol: str
    asset_class: str
    mysql_signed_qty: float
    broker_signed_qty: float
    delta: float  # broker_signed_qty - mysql_signed_qty
    mysql_rows: list[dict] = field(default_factory=list)
    broker_position: dict | None = None
    suggested_flatten_side: str = ""  # 'sell' or 'buy'
    suggested_flatten_qty: float = 0.0


def aggregate_mysql_signed_qty(
    rows: Iterable[dict],
) -> dict[tuple[str, str], dict]:
    """Group MySQL open rows by (symbol, asset_class) and compute signed qty.

    Returns a dict keyed by (symbol, asset_class) with:
      - signed_qty: sum(qty if side=='long' else -qty)
      - rows: list of the original dicts (for the report's per-row detail)
    """
    out: dict[tuple[str, str], dict] = {}
    for r in rows:
        key = (r["symbol"], r["asset_class"])
        bucket = out.setdefault(key, {"signed_qty": 0.0, "rows": []})
        sign = 1.0 if r["side"] == "long" else -1.0
        bucket["signed_qty"] += sign * float(r["qty"])
        bucket["rows"].append(r)
    return out


def broker_signed_qty(pos: dict) -> float:
    """Alpaca returns position qty as a string; sign is in the side field
    (the qty string itself can be negative for shorts on equity, but is
    always positive on crypto). Use side as the source of truth."""
    qty = abs(float(pos["qty"]))
    return qty if pos["side"] == "long" else -qty


def _broker_asset_class(pos: dict) -> str:
    """Alpaca emits 'us_equity' / 'crypto' — normalize to our internal
    'equity' / 'crypto' values used in MySQL.asset_class."""
    ac = pos.get("asset_class") or ""
    return "crypto" if ac == "crypto" else "equity"


def detect_drift(
    mysql_rows: list[dict],
    broker_positions: list[dict],
) -> list[DriftRow]:
    """Return a DriftRow per (symbol, asset_class) where MySQL signed qty
    disagrees with broker signed qty. Includes asymmetric cases (mysql-only
    or broker-only).

    Suggested flatten qty is `abs(delta) = abs(broker_signed - mysql_signed)`,
    side is sell if delta>0 (broker too long; sell to bring it down to mysql),
    buy if delta<0. Operator decides whether MySQL is the right target.
    """
    mysql_agg = aggregate_mysql_signed_qty(mysql_rows)
    broker_by_key: dict[tuple[str, str], dict] = {}
    for p in broker_positions:
        key = (p["symbol"], _broker_asset_class(p))
        prev = broker_by_key.get(key)
        if prev is None:
            broker_by_key[key] = p
        else:
            prev_qty = broker_signed_qty(prev)
            this_qty = broker_signed_qty(p)
            total = prev_qty + this_qty
            broker_by_key[key] = {
                **prev,
                "qty": str(abs(total)),
                "side": "long" if total >= 0 else "short",
            }

    out: list[DriftRow] = []
    all_keys = set(mysql_agg.keys()) | set(broker_by_key.keys())
    for key in sorted(all_keys):
        symbol, asset_class = key
        mysql_q = mysql_agg.get(key, {}).get("signed_qty", 0.0)
        bp = broker_by_key.get(key)
        broker_q = broker_signed_qty(bp) if bp else 0.0
        if mysql_q == broker_q:
            continue
        delta = broker_q - mysql_q
        suggested_qty = abs(delta)
        suggested_side = "sell" if delta > 0 else "buy"
        out.append(DriftRow(
            symbol=symbol,
            asset_class=asset_class,
            mysql_signed_qty=mysql_q,
            broker_signed_qty=broker_q,
            delta=delta,
            mysql_rows=mysql_agg.get(key, {}).get("rows", []),
            broker_position=bp,
            suggested_flatten_side=suggested_side,
            suggested_flatten_qty=suggested_qty,
        ))
    return out


def format_report(drifts: list[DriftRow]) -> str:
    if not drifts:
        return "no drift detected — MySQL and broker agree on every symbol."
    lines: list[str] = []
    for d in drifts:
        lines.append(f"DRIFT symbol={d.symbol} asset_class={d.asset_class}")
        lines.append(
            f"  mysql_open: {len(d.mysql_rows)} row(s) — "
            f"net signed qty = {d.mysql_signed_qty:+g}"
        )
        for r in d.mysql_rows:
            lines.append(
                f"    id={r['id']} setup={r['setup_name']} "
                f"side={r['side']} qty={r['qty']} "
                f"opened_at={r['opened_at']} "
                f"coid={r.get('client_order_id') or '<none>'}"
            )
        if d.broker_position:
            bp = d.broker_position
            lines.append(
                f"  broker: side={bp['side']} qty={bp['qty']} "
                f"avg_entry={bp.get('avg_entry_price', '?')} "
                f"— net signed qty = {d.broker_signed_qty:+g}"
            )
        else:
            lines.append("  broker: no matching position")
        lines.append(f"  drift: broker_signed - mysql_signed = {d.delta:+g}")
        lines.append(
            f"  suggested_manual_flatten: side={d.suggested_flatten_side} "
            f"qty={d.suggested_flatten_qty:g}"
        )
        lines.append("")
    return "\n".join(lines)


def _fetch_mysql_rows() -> list[dict]:
    """Pull every open PositionRow across all strategies on this MySQL."""
    engine = create_engine(_build_url())
    with Session(engine) as session:
        rows = session.query(PositionRow).filter(
            PositionRow.status == "open",
        ).all()
        return [{
            "id": r.id,
            "symbol": r.symbol,
            "asset_class": r.asset_class,
            "side": r.side,
            "qty": float(r.qty),
            "setup_name": r.setup_name,
            "opened_at": r.opened_at.isoformat() if r.opened_at else "",
            "client_order_id": r.client_order_id or "",
        } for r in rows]


def main() -> int:
    try:
        mysql_rows = _fetch_mysql_rows()
    except Exception as exc:
        print(f"ERROR fetching MySQL rows: {exc}", file=sys.stderr)
        return 0  # report-only, never fail

    try:
        router = AlpacaRouter()
        broker_positions = router.get_positions()
    except Exception as exc:
        print(f"ERROR fetching broker positions: {exc}", file=sys.stderr)
        return 0

    drifts = detect_drift(mysql_rows, broker_positions)
    print(format_report(drifts))
    return 0


if __name__ == "__main__":
    sys.exit(main())
