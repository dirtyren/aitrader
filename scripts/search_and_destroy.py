#!/usr/bin/env python3
"""HIGHLY DESTRUCTIVE: close every Alpaca position and wipe MySQL state.

Use this to start from a clean slate after a configuration change or
incident. The script:

  1. Lists every open broker order and cancels it (so brackets / TPs /
     stops don't fight the close orders we're about to submit).
  2. Submits a market order to close every open broker position
     (sell-to-close longs, buy-to-cover shorts), tagged with a synthetic
     role=exit COID so any leftover reconciler watching can attribute it.
  3. Polls /v2/positions for up to N seconds and records the final state.
  4. Writes runtime/clean_slate_audit_<timestamp>.jsonl with the full
     before/after snapshot — survives the MySQL truncation that follows.
  5. Truncates MySQL: positions, trades, daily_stats,
     reconciliation_strikes, reconciliation_events. Preserves strategies
     so trader containers keep their FK targets.

Usage:
  docker compose exec trader python scripts/search_and_destroy.py --dry-run
  docker compose exec trader python scripts/search_and_destroy.py \
      --apply --confirm-account <ACCOUNT_NUMBER>

The --confirm-account argument MUST match the Alpaca account number
returned by /v2/account. This prevents misfiring against a different
account by mistake (e.g., if env vars were swapped).

Exit codes:
   0   success
   1   user cancelled / argparse error
   2   account confirmation mismatch
   3   broker close failures (some positions not closed)
   4   MySQL truncation failed
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone

# Allow `python scripts/search_and_destroy.py` from repo root.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from sqlalchemy import inspect as _sql_inspect, text as _sql_text
from sqlalchemy.orm import Session

from broker.alpaca_client import AlpacaClient
from broker.client_order_id import Role, make_client_order_id
from state.mysql_store import MySQLStore


_TRUNCATE_TABLES = (
    "reconciliation_events",   # FK to strategies (loose)
    "reconciliation_strikes",  # FK to strategies (loose)
    "trades",                  # FK to strategies
    "positions",               # FK to strategies
    "daily_stats",             # FK to strategies
)


# ── argparse ─────────────────────────────────────────────────────────


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="search_and_destroy",
        description=(
            "DESTRUCTIVE: close every Alpaca position and wipe MySQL "
            "(positions, trades, daily_stats, reconciliation_*). "
            "Preserves the strategies table."
        ),
    )
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true",
                   help="Show what would happen, write nothing.")
    g.add_argument("--apply", action="store_true",
                   help="Actually run the wipe. Requires --confirm-account.")
    p.add_argument("--confirm-account", default=None,
                   help="Alpaca account number — must match /v2/account.id.")
    p.add_argument("--poll-seconds", type=int, default=20,
                   help="Seconds to poll /v2/positions after close (default 20).")
    return p


# ── helpers ──────────────────────────────────────────────────────────


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _audit_path() -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"runtime/clean_slate_audit_{ts}.jsonl"


def _make_exit_coid(symbol: str) -> str:
    """Synthetic role=exit COID with a 'cleanslate' setup tag for traceability."""
    return make_client_order_id("operator", "cleanslate", symbol, Role.EXIT)


def _alpaca_side_to_close(broker_side: str) -> str:
    return "sell" if broker_side == "long" else "buy"


def _flatten_position(alpaca: AlpacaClient, p: dict, audit: list[dict]) -> dict:
    """Submit a market order to close one broker position."""
    symbol = p["symbol"]
    qty = abs(float(p["qty"]))
    side_to_close = _alpaca_side_to_close(p["side"])
    coid = _make_exit_coid(symbol)
    record: dict = {
        "ts": _now_iso(),
        "action": "submit_close",
        "symbol": symbol,
        "broker_qty": p["qty"],
        "broker_side": p["side"],
        "close_order_side": side_to_close,
        "close_order_qty": qty,
        "client_order_id": coid,
    }
    try:
        order = alpaca.submit_order(
            symbol=symbol, qty=qty, side=side_to_close,
            order_type="market", time_in_force="gtc",
            client_order_id=coid,
        )
        record["alpaca_order_id"] = order.get("id")
        record["status"] = "submitted"
    except Exception as exc:
        record["status"] = "submit_failed"
        record["error"] = str(exc)
    audit.append(record)
    return record


def _cancel_all_orders(alpaca: AlpacaClient, audit: list[dict]) -> int:
    """Cancel all open orders. Returns count cancelled."""
    try:
        orders = alpaca.list_orders(status="open", nested=False)
    except Exception as exc:
        audit.append({
            "ts": _now_iso(), "action": "list_open_orders_failed",
            "error": str(exc),
        })
        return 0
    cancelled = 0
    for o in orders:
        oid = o.get("id")
        if not oid:
            continue
        try:
            alpaca.cancel_order(oid)
            audit.append({
                "ts": _now_iso(), "action": "cancel_order",
                "alpaca_order_id": oid, "symbol": o.get("symbol"),
                "status": "cancelled",
            })
            cancelled += 1
        except Exception as exc:
            audit.append({
                "ts": _now_iso(), "action": "cancel_order",
                "alpaca_order_id": oid, "symbol": o.get("symbol"),
                "status": "cancel_failed", "error": str(exc),
            })
    return cancelled


def _poll_until_flat(
    alpaca: AlpacaClient, deadline_s: int, audit: list[dict],
) -> list[dict]:
    """Poll /v2/positions for up to deadline_s. Returns final position list."""
    deadline = time.time() + deadline_s
    last: list[dict] = []
    while time.time() < deadline:
        try:
            last = alpaca.get_positions()
        except Exception as exc:
            audit.append({
                "ts": _now_iso(), "action": "poll_positions_failed",
                "error": str(exc),
            })
            time.sleep(2)
            continue
        if not last:
            return []
        time.sleep(2)
    return last


def _mysql_row_counts(store: MySQLStore) -> dict[str, int]:
    out: dict[str, int] = {}
    with Session(store._engine) as session:
        for table in _TRUNCATE_TABLES + ("strategies",):
            try:
                row = session.execute(_sql_text(
                    f"SELECT COUNT(*) FROM {table}"
                )).first()
                out[table] = int(row[0]) if row else 0
            except Exception:
                out[table] = -1  # table missing or error
    return out


def _truncate_mysql(store: MySQLStore, audit: list[dict]) -> bool:
    """Wipe rows from the target tables. Returns True on success.

    Uses DELETE FROM (transactional, dialect-portable) rather than TRUNCATE
    (DDL, MySQL-only fast-path). DELETE means we can roll back inside the
    session if any one statement fails. On MySQL, FK checks are disabled
    around the loop so child→parent ordering doesn't matter.
    """
    is_mysql = store._engine.dialect.name == "mysql"
    existing = set(_sql_inspect(store._engine).get_table_names())
    with Session(store._engine) as session:
        try:
            if is_mysql:
                session.execute(_sql_text("SET FOREIGN_KEY_CHECKS = 0"))
            for table in _TRUNCATE_TABLES:
                if table not in existing:
                    audit.append({
                        "ts": _now_iso(), "action": "truncate", "table": table,
                        "status": "skipped_table_missing",
                    })
                    continue
                session.execute(_sql_text(f"DELETE FROM {table}"))
                audit.append({
                    "ts": _now_iso(), "action": "truncate", "table": table,
                    "status": "ok",
                })
            if is_mysql:
                session.execute(_sql_text("SET FOREIGN_KEY_CHECKS = 1"))
            session.commit()
            return True
        except Exception as exc:
            audit.append({
                "ts": _now_iso(), "action": "truncate_failed",
                "error": str(exc),
            })
            try:
                session.rollback()
                if is_mysql:
                    session.execute(_sql_text("SET FOREIGN_KEY_CHECKS = 1"))
                    session.commit()
            except Exception:
                pass
            return False


def _write_audit(path: str, audit: list[dict]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        for record in audit:
            f.write(json.dumps(record) + "\n")


# ── main ─────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    alpaca = AlpacaClient()
    store = MySQLStore(strategy_name="operator")
    store.ensure_schema()
    store.upsert_strategy()

    # Snapshot account + positions + MySQL counts before doing anything.
    try:
        account = alpaca.get_account()
    except Exception as exc:
        print(f"error: cannot reach Alpaca: {exc}", file=sys.stderr)
        return 1
    account_id = account.get("id", "")
    account_number = account.get("account_number", "")
    print(
        f"Connected to Alpaca account: id={account_id} "
        f"account_number={account_number}"
    )

    try:
        positions = alpaca.get_positions()
    except Exception as exc:
        print(f"error: cannot list positions: {exc}", file=sys.stderr)
        return 1

    counts_before = _mysql_row_counts(store)
    print(f"\n{len(positions)} broker position(s):")
    for p in positions:
        print(
            f"  symbol={p['symbol']:<10} qty={p['qty']:<22} "
            f"side={p['side']:<6} class={p.get('asset_class')}"
        )
    print(f"\nMySQL row counts: {counts_before}")

    if args.dry_run:
        print(
            "\n(dry-run) would: cancel all open orders, market-close all "
            f"{len(positions)} positions, then truncate MySQL tables: "
            f"{', '.join(_TRUNCATE_TABLES)}"
        )
        return 0

    # --apply path requires the typed confirmation.
    if args.confirm_account is None:
        print(
            "\nerror: --apply requires --confirm-account=<ACCOUNT_NUMBER>",
            file=sys.stderr,
        )
        print(
            f"Run with: --confirm-account {account_number}",
            file=sys.stderr,
        )
        return 2

    if args.confirm_account != account_number:
        print(
            f"\nerror: --confirm-account={args.confirm_account!r} does not "
            f"match Alpaca account_number={account_number!r}",
            file=sys.stderr,
        )
        return 2

    print("\n=== APPLYING — confirmation matched, proceeding ===\n")
    audit: list[dict] = [{
        "ts": _now_iso(), "action": "begin",
        "alpaca_account_id": account_id,
        "alpaca_account_number": account_number,
        "broker_positions_before": positions,
        "mysql_counts_before": counts_before,
    }]

    # 1. Cancel all open orders.
    cancelled = _cancel_all_orders(alpaca, audit)
    print(f"cancelled {cancelled} open order(s)")

    # 2. Submit market closes for every position.
    for p in positions:
        rec = _flatten_position(alpaca, p, audit)
        print(
            f"  close {rec['symbol']:<10} "
            f"qty={rec['close_order_qty']:<22} side={rec['close_order_side']:<5} "
            f"-> {rec['status']}"
        )

    # 3. Poll until flat (or timeout).
    print(f"\npolling /v2/positions for up to {args.poll_seconds}s...")
    remaining = _poll_until_flat(alpaca, args.poll_seconds, audit)
    if remaining:
        print(
            f"\nWARNING: {len(remaining)} position(s) still open after "
            f"{args.poll_seconds}s:"
        )
        for p in remaining:
            print(f"  {p['symbol']} qty={p['qty']} side={p['side']}")
        audit.append({
            "ts": _now_iso(), "action": "broker_not_flat",
            "remaining": remaining,
        })
        # Write audit early — don't lose it if truncation also fails.
        path = _audit_path()
        _write_audit(path, audit)
        print(f"\naudit written to {path}")
        print("\nNot truncating MySQL — broker is not flat. Re-run after "
              "manually closing remaining positions.")
        return 3
    print("broker is flat.")

    # 4. Truncate MySQL.
    print(f"\ntruncating MySQL tables: {', '.join(_TRUNCATE_TABLES)}")
    ok = _truncate_mysql(store, audit)
    counts_after = _mysql_row_counts(store)
    audit.append({
        "ts": _now_iso(), "action": "mysql_counts_after",
        "counts": counts_after,
    })

    # 5. Write audit (always).
    path = _audit_path()
    _write_audit(path, audit)
    print(f"\naudit written to {path}")
    print(f"MySQL row counts after: {counts_after}")

    if not ok:
        print("\nMySQL truncation FAILED — see audit", file=sys.stderr)
        return 4

    print("\nclean slate complete. broker flat, MySQL truncated, "
          "audit preserved.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
