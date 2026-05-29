#!/usr/bin/env python3
"""One-shot: backfill synthetic role=adopted COIDs onto legacy_untagged rows.

Pre-Plan-2 positions (those open before COID minting was introduced) are
flagged with `legacy_untagged=1` and have NULL `client_order_id`. This
script gives each one a synthetic COID of the form:

    aitrader__<strategy>__<setup>__<symbol>__adopted__<uuid8>

After the backfill:
- `client_order_id` is non-NULL (so MySQLStore.find_open_position_by_coid
  can find it; useful for operator CLI / Plan 4 paths).
- `legacy_untagged` is cleared to 0 — the row is no longer second-class.

What this DOES NOT fix:
- Alpaca never saw these COIDs (the orders were submitted before Plan 2).
  So if a broker fill reports the original (pre-Plan-2) Alpaca order id,
  the reconciler still can't match it via parse_client_order_id.
- This is an internal-state cleanup, not a true broker reconciliation.

Usage:
    docker compose exec trader python scripts/backfill_legacy_coids.py --dry-run
    docker compose exec trader python scripts/backfill_legacy_coids.py --apply
"""
from __future__ import annotations

import argparse
import os
import sys

# Allow `python scripts/backfill_legacy_coids.py` from repo root.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from sqlalchemy.orm import Session

from broker.client_order_id import Role, make_client_order_id
from state.mysql_store import EventRow, MySQLStore, PositionRow, StrategyRow


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="backfill_legacy_coids",
        description=(
            "Stamp synthetic role=adopted COIDs on legacy_untagged open rows."
        ),
    )
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true",
                   help="Print what would change without writing.")
    g.add_argument("--apply", action="store_true",
                   help="Apply the backfill to MySQL.")
    return p


def _candidates(session: Session) -> list[tuple[PositionRow, str]]:
    """Return (row, strategy_name) pairs eligible for backfill."""
    rows = session.query(PositionRow, StrategyRow.name).join(
        StrategyRow, StrategyRow.id == PositionRow.strategy_id,
    ).filter(
        PositionRow.status == "open",
        PositionRow.legacy_untagged == True,  # noqa: E712
        PositionRow.client_order_id.is_(None),
    ).all()
    return [(row, name) for row, name in rows]


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    store = MySQLStore(strategy_name="operator")
    store.ensure_schema()
    store.upsert_strategy()

    with Session(store._engine) as session:
        candidates = _candidates(session)
        if not candidates:
            print("(no legacy_untagged rows to backfill)")
            return 0

        print(f"{len(candidates)} legacy_untagged row(s) eligible for backfill:")
        plan: list[tuple[int, str]] = []  # (row_id, coid)
        for row, strategy_name in candidates:
            coid = make_client_order_id(
                strategy_name, row.setup_name, row.symbol, Role.ADOPTED,
            )
            plan.append((row.id, coid))
            print(
                f"  position #{row.id:<4} strategy={strategy_name:<22} "
                f"symbol={row.symbol:<10} setup={row.setup_name:<14} "
                f"-> coid={coid}"
            )

        if args.dry_run:
            print("(dry-run — no changes written)")
            return 0

        # Apply.
        for row_id, coid in plan:
            row = session.query(PositionRow).filter(
                PositionRow.id == row_id
            ).one()
            row.client_order_id = coid
            row.legacy_untagged = False
            session.add(EventRow(
                type="operator_action",
                strategy_id=row.strategy_id,
                symbol=row.symbol,
                payload={
                    "operator_action": "backfill_legacy_coid",
                    "position_id": row_id,
                    "client_order_id": coid,
                    "operator_note": "backfill_legacy_coids.py --apply",
                },
            ))
        session.commit()
        print(f"applied {len(plan)} row(s); legacy_untagged cleared, COIDs set, "
              f"audit events written.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
