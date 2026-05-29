#!/usr/bin/env python3
"""One-shot: close duplicate open positions that share a client_order_id.

Background: prior to the find_open_position_by_coid fix, the reconciler
crashed with MultipleResultsFound whenever two open rows shared a COID.
The crash happened *before* the idempotency check returned, so the
entry-recovery branch kept inserting another row each cycle — a feedback
loop that produced strings of duplicates for the same COID.

This script keeps the OLDEST open row per (client_order_id) tuple and
closes every other open row sharing that COID with close_reason=
'duplicate_dedupe'. The kept row is the one with the smallest opened_at
(equivalently the smallest id when timestamps tie).

The duplicate rows are closed in the positions table (status='closed',
exit_px = entry_px, pnl_usd = 0, R_realized = 0) and a corresponding
trades row is written so audit history is preserved. A reconciliation_event
of type 'duplicate_dedupe_applied' is emitted per closed row.

Idempotent: running it again after a clean run finds nothing to do.

Usage:
    docker compose exec trader python scripts/dedupe_duplicate_coids.py --dry-run
    docker compose exec trader python scripts/dedupe_duplicate_coids.py --apply
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from decimal import Decimal

# Allow `python scripts/dedupe_duplicate_coids.py` from repo root.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from sqlalchemy import func
from sqlalchemy.orm import Session

from state.mysql_store import EventRow, MySQLStore, PositionRow, TradeRow


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="dedupe_duplicate_coids",
        description=(
            "Close duplicate open positions sharing a client_order_id; "
            "keep the oldest row per COID."
        ),
    )
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true",
                   help="Print what would change without writing.")
    g.add_argument("--apply", action="store_true",
                   help="Apply the dedupe to MySQL.")
    return p


def _duplicate_coids(session: Session) -> list[str]:
    """Return COIDs that have more than one open position row."""
    rows = (
        session.query(PositionRow.client_order_id)
        .filter(
            PositionRow.status == "open",
            PositionRow.client_order_id.isnot(None),
        )
        .group_by(PositionRow.client_order_id)
        .having(func.count(PositionRow.id) > 1)
        .all()
    )
    return [r[0] for r in rows]


def _open_rows_for_coid(session: Session, coid: str) -> list[PositionRow]:
    """All open rows for this COID, oldest first (kept row at index 0)."""
    return (
        session.query(PositionRow)
        .filter(
            PositionRow.client_order_id == coid,
            PositionRow.status == "open",
        )
        .order_by(PositionRow.opened_at.asc(), PositionRow.id.asc())
        .all()
    )


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    store = MySQLStore(strategy_name="operator")
    store.ensure_schema()
    store.upsert_strategy()

    with Session(store._engine) as session:
        coids = _duplicate_coids(session)
        if not coids:
            print("(no duplicate-COID open rows found)")
            return 0

        total_to_close = 0
        plan: list[tuple[str, PositionRow, list[PositionRow]]] = []
        for coid in coids:
            rows = _open_rows_for_coid(session, coid)
            keep, close = rows[0], rows[1:]
            plan.append((coid, keep, close))
            total_to_close += len(close)
            print(
                f"COID={coid}\n"
                f"  KEEP  position #{keep.id} opened_at={keep.opened_at}\n"
                + "".join(
                    f"  CLOSE position #{r.id} opened_at={r.opened_at}\n"
                    for r in close
                )
            )

        print(f"summary: {len(coids)} duplicate COID group(s); "
              f"{total_to_close} row(s) to close.")

        if args.dry_run:
            print("(dry-run — no changes written)")
            return 0

        now = datetime.now(timezone.utc)
        for coid, keep, close in plan:
            for row in close:
                # Mirror position_closed semantics — exit at entry, zero PnL.
                row.status = "closed"
                row.exit_px = row.entry_px
                row.pnl_usd = Decimal("0")
                row.R_realized = Decimal("0")
                row.close_reason = "duplicate_dedupe"
                row.closed_at = now

                session.add(TradeRow(
                    strategy_id=row.strategy_id,
                    symbol=row.symbol,
                    asset_class=row.asset_class,
                    setup_name=row.setup_name,
                    side=row.side,
                    qty=row.qty,
                    entry_px=row.entry_px,
                    exit_px=row.entry_px,
                    stop_px=row.stop_px,
                    target_px=row.target_px,
                    initial_stop_px=row.initial_stop_px,
                    pnl_usd=Decimal("0"),
                    R_realized=Decimal("0"),
                    close_reason="duplicate_dedupe",
                    opened_at=row.opened_at,
                    closed_at=now,
                    bars_held=row.bars_held,
                    client_order_id=row.client_order_id,
                    exit_client_order_id=None,
                ))

                session.add(EventRow(
                    type="duplicate_dedupe_applied",
                    strategy_id=row.strategy_id,
                    symbol=row.symbol,
                    payload={
                        "client_order_id": coid,
                        "closed_position_id": row.id,
                        "kept_position_id": keep.id,
                    },
                ))

        session.commit()
        print(f"applied: closed {total_to_close} duplicate row(s); "
              f"trades + events written.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
