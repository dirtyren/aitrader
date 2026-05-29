#!/usr/bin/env python3
"""Operator CLI for the reconciliation v2 strike-and-event surface.

Subcommands:
  list                                    — show all unresolved strikes
  show <id>                               — full detail for one strike
  close <id> --exit-px <px> --reason <r>  — close a mysql_only position
                  --setup <name> --note <text>
  force-zero <id> --setup <name> --note <text>
                                          — close as pnl=0, reason='reconciled_gone'
  adopt <id> --strategy <s> --setup <s>   — adopt a broker_only orphan as a
                  --side <long|short> --qty <q> --entry-px <p>
                  --asset-class <equity|crypto> --note <text>
  extend <id> --note <text>               — reset strike_count, reopen the case
  dismiss <id> --note <text>              — resolve without action
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

# Allow running as `python scripts/reconcile_resolve.py` directly from the
# repo root by ensuring the project root is on sys.path.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from sqlalchemy.orm import Session

from broker.client_order_id import Role, make_client_order_id
from state.mysql_store import EventRow, MySQLStore, StrategyRow, StrikeRow


# ── output helpers ───────────────────────────────────────────────────


def _print_strike_row(s, verbose: bool = False) -> None:
    print(
        f"#{s.id:<5} {s.direction:<12} {s.symbol:<10} "
        f"strike={s.strike_count:<2} last_seen={s.last_seen_at.isoformat()}"
    )
    if verbose:
        print(f"    key       = {s.key}")
        print(f"    strategy  = {s.strategy_id}")
        print(f"    first_seen= {s.first_seen_at.isoformat()}")
        snap = s.last_observed_state
        if isinstance(snap, str):
            try:
                snap = json.loads(snap)
            except json.JSONDecodeError:
                pass
        print(f"    snapshot  = {snap}")


def _resolve_strategy_id_or_exit(store: MySQLStore, name: str) -> int:
    with Session(store._engine) as session:
        row = session.query(StrategyRow).filter(StrategyRow.name == name).one_or_none()
    if row is None:
        print(f"error: unknown strategy {name!r}", file=sys.stderr)
        raise SystemExit(2)
    return row.id


def _get_strike_or_exit(store: MySQLStore, strike_id: int):
    s = store.get_strike_by_id(strike_id)
    if s is None:
        print(f"error: strike #{strike_id} not found", file=sys.stderr)
        raise SystemExit(2)
    return s


def _ensure_direction(strike, expected: str) -> None:
    if strike.direction != expected:
        print(
            f"error: strike #{strike.id} has direction={strike.direction!r}, "
            f"expected {expected!r}",
            file=sys.stderr,
        )
        raise SystemExit(2)


# ── subcommands ──────────────────────────────────────────────────────


def cmd_list(store: MySQLStore) -> None:
    rows = store.list_unresolved_strikes()
    if not rows:
        print("(no unresolved strikes)")
        return
    print(f"{len(rows)} unresolved strike(s):")
    for s in rows:
        _print_strike_row(s, verbose=False)


def cmd_show(store: MySQLStore, strike_id: int) -> None:
    s = _get_strike_or_exit(store, strike_id)
    print(f"strike #{s.id}:")
    _print_strike_row(s, verbose=True)
    events = store.events_for_strike(s, limit=20)
    print(f"  recent events ({len(events)}):")
    for e in events:
        print(f"    {e.created_at.isoformat()}  type={e.type}  payload={e.payload}")


def cmd_close(
    store: MySQLStore, *, strike_id: int, exit_px: float, reason: str,
    setup: str, note: str,
) -> None:
    s = _get_strike_or_exit(store, strike_id)
    _ensure_direction(s, "mysql_only")
    if s.strategy_id is None:
        print(f"error: strike #{strike_id} has no strategy_id", file=sys.stderr)
        raise SystemExit(2)
    result = store.position_closed(
        symbol=s.symbol, exit_px=exit_px, close_reason=reason,
        setup_name=setup, strategy_id=s.strategy_id,
    )
    if result is None:
        print(f"error: no open position for strategy_id={s.strategy_id} "
              f"symbol={s.symbol} setup={setup}", file=sys.stderr)
        raise SystemExit(2)
    store.resolve_strike(strike_id, reason="operator_closed_position",
                         operator_note=note)
    print(f"closed position symbol={s.symbol} setup={setup} "
          f"exit_px={exit_px} pnl={result['pnl_usd']:.2f}")
    print(f"resolved strike #{strike_id}")


def cmd_force_zero(
    store: MySQLStore, *, strike_id: int, setup: str, note: str,
) -> None:
    s = _get_strike_or_exit(store, strike_id)
    _ensure_direction(s, "mysql_only")
    if s.strategy_id is None:
        print(f"error: strike #{strike_id} has no strategy_id", file=sys.stderr)
        raise SystemExit(2)
    open_row = store.find_open_position_by_setup(s.strategy_id, s.symbol, setup)
    if open_row is None:
        print(f"error: no open position to force-zero", file=sys.stderr)
        raise SystemExit(2)
    result = store.position_closed(
        symbol=s.symbol, exit_px=float(open_row.entry_px),
        close_reason="reconciled_gone", setup_name=setup,
        strategy_id=s.strategy_id,
    )
    if result is None:
        print("error: position_closed returned None", file=sys.stderr)
        raise SystemExit(2)
    store.resolve_strike(strike_id, reason="operator_force_zero",
                         operator_note=note)
    print(f"force-closed position symbol={s.symbol} setup={setup} pnl=0")
    print(f"resolved strike #{strike_id}")


def cmd_adopt(
    store: MySQLStore, *, strike_id: int, strategy_name: str, setup: str,
    side: str, qty: float, entry_px: float, asset_class: str, note: str,
) -> None:
    s = _get_strike_or_exit(store, strike_id)
    _ensure_direction(s, "broker_only")
    strategy_id = _resolve_strategy_id_or_exit(store, strategy_name)
    coid = make_client_order_id(strategy_name, setup, s.symbol, Role.ADOPTED)
    new_pos_id = store.insert_adopted_position(
        strategy_id=strategy_id, setup_name=setup, symbol=s.symbol,
        side=side, qty=qty, entry_px=entry_px, asset_class=asset_class,
        opened_at=datetime.now(timezone.utc), client_order_id=coid,
    )
    store.resolve_strike(strike_id, reason="operator_adopted",
                         operator_note=note)
    print(f"adopted position id={new_pos_id} symbol={s.symbol} "
          f"strategy={strategy_name} setup={setup} coid={coid}")
    print(f"resolved strike #{strike_id}")


def cmd_extend(store: MySQLStore, *, strike_id: int, note: str) -> None:
    _get_strike_or_exit(store, strike_id)  # validate id
    with Session(store._engine) as session:
        row = session.query(StrikeRow).filter(StrikeRow.id == strike_id).one_or_none()
        if row is None or row.resolved:
            print(f"error: strike #{strike_id} not found or already resolved",
                  file=sys.stderr)
            raise SystemExit(2)
        row.strike_count = 0
        session.add(EventRow(
            type="operator_action",
            strategy_id=row.strategy_id,
            symbol=row.symbol,
            payload={
                "strike_id": strike_id, "key": row.key,
                "operator_action": "extend",
                "operator_note": note,
            },
        ))
        session.commit()
    print(f"extended strike #{strike_id} (count reset to 0, still unresolved)")


def cmd_dismiss(store: MySQLStore, *, strike_id: int, note: str) -> None:
    _get_strike_or_exit(store, strike_id)
    ok = store.resolve_strike(strike_id, reason="operator_dismissed",
                              operator_note=note)
    if not ok:
        print(f"error: strike #{strike_id} could not be dismissed",
              file=sys.stderr)
        raise SystemExit(2)
    print(f"dismissed strike #{strike_id}")


# ── argparse wiring ──────────────────────────────────────────────────


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="reconcile_resolve",
        description="Operator CLI for the reconciliation v2 strike surface.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list")

    sp = sub.add_parser("show")
    sp.add_argument("strike_id", type=int)

    sp = sub.add_parser("close")
    sp.add_argument("strike_id", type=int)
    sp.add_argument("--exit-px", type=float, required=True)
    sp.add_argument("--reason", default="operator_closed_position")
    sp.add_argument("--setup", required=True)
    sp.add_argument("--note", required=True)

    sp = sub.add_parser("force-zero")
    sp.add_argument("strike_id", type=int)
    sp.add_argument("--setup", required=True)
    sp.add_argument("--note", required=True)

    sp = sub.add_parser("adopt")
    sp.add_argument("strike_id", type=int)
    sp.add_argument("--strategy", required=True)
    sp.add_argument("--setup", required=True)
    sp.add_argument("--side", choices=["long", "short"], required=True)
    sp.add_argument("--qty", type=float, required=True)
    sp.add_argument("--entry-px", type=float, required=True)
    sp.add_argument("--asset-class", choices=["equity", "crypto"], required=True)
    sp.add_argument("--note", required=True)

    sp = sub.add_parser("extend")
    sp.add_argument("strike_id", type=int)
    sp.add_argument("--note", required=True)

    sp = sub.add_parser("dismiss")
    sp.add_argument("strike_id", type=int)
    sp.add_argument("--note", required=True)

    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    store = MySQLStore(strategy_name="operator")
    store.ensure_schema()
    store.upsert_strategy()

    if args.cmd == "list":
        cmd_list(store)
    elif args.cmd == "show":
        cmd_show(store, strike_id=args.strike_id)
    elif args.cmd == "close":
        cmd_close(store, strike_id=args.strike_id, exit_px=args.exit_px,
                  reason=args.reason, setup=args.setup, note=args.note)
    elif args.cmd == "force-zero":
        cmd_force_zero(store, strike_id=args.strike_id, setup=args.setup,
                       note=args.note)
    elif args.cmd == "adopt":
        cmd_adopt(store, strike_id=args.strike_id,
                  strategy_name=args.strategy, setup=args.setup,
                  side=args.side, qty=args.qty, entry_px=args.entry_px,
                  asset_class=args.asset_class, note=args.note)
    elif args.cmd == "extend":
        cmd_extend(store, strike_id=args.strike_id, note=args.note)
    elif args.cmd == "dismiss":
        cmd_dismiss(store, strike_id=args.strike_id, note=args.note)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
