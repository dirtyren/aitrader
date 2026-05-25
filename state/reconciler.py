from __future__ import annotations
from typing import Iterable


def _normalize_asset_class(raw: str | None) -> str | None:
    """Map Alpaca's asset_class strings to the codebase's canonical names.

    "us_equity" -> "equity"
    "crypto"    -> "crypto"
    anything else -> None (caller logs and skips)
    """
    if raw is None:
        return None
    s = raw.strip().lower()
    if s == "us_equity":
        return "equity"
    if s == "crypto":
        return "crypto"
    return None


def _normalize_side(raw: str) -> str:
    """Alpaca position side is 'long' or 'short' — pass through, defended."""
    s = (raw or "").strip().lower()
    if s in ("long", "short"):
        return s
    raise ValueError(f"Unexpected position side: {raw!r}")


def _index_bracket_children(orders: Iterable[dict]) -> dict[str, dict]:
    """Index open bracket children by symbol from a flat list of orders.

    Handles two shapes: (a) parent order with legs nested under `legs`,
    (b) orphaned children appearing as top-level orders with `parent_id` set.

    Returns: {symbol: {"stop": leg_dict | None, "target": leg_dict | None}}
    """
    out: dict[str, dict] = {}

    def _classify(order: dict) -> tuple[str | None, dict] | None:
        otype = (order.get("type") or "").lower()
        symbol = order.get("symbol")
        if symbol is None:
            return None
        if otype in ("stop", "stop_limit"):
            return ("stop", order)
        if otype == "limit" and order.get("limit_price") is not None:
            return ("target", order)
        return None

    for order in orders:
        legs = order.get("legs") or []
        candidates = list(legs) if legs else [order]
        for cand in candidates:
            classified = _classify(cand)
            if classified is None:
                continue
            kind, leg = classified
            symbol = leg["symbol"]
            slot = out.setdefault(symbol, {"stop": None, "target": None})
            if slot[kind] is None:
                slot[kind] = leg

    return out


import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from state.position_book import OpenPosition, PositionBook


@dataclass
class ReconcileReport:
    closed: list[str] = field(default_factory=list)
    adopted_equity: list[str] = field(default_factory=list)
    adopted_crypto: list[str] = field(default_factory=list)
    drift: list[tuple[str, float, float]] = field(default_factory=list)
    equity_no_bracket: list[str] = field(default_factory=list)


_QTY_EPS = 1e-6


class Reconciler:
    """Reconciles the in-memory PositionBook against Alpaca's /v2/positions.

    Policy (see spec 2026-05-22-broker-position-reconciliation-design.md):
    - Closed (in book, not in broker): book.close(symbol).
    - Drift (qty differs): log only, no mutation.
    - Orphan (in broker, not in book): adopt as monitor-only with
      adopted=True. Equity adoptions recover stop/target/stop_order_id from
      the live bracket children (or live with all three None and a
      RECONCILE_EQUITY_NO_BRACKET warning). Crypto adoptions are naked.
    """

    def __init__(self, alpaca, ac_configs: dict | None = None,
                 *, logger: logging.Logger | None = None) -> None:
        self._alpaca = alpaca
        self._ac_configs = ac_configs or {}
        self._log = logger or logging.getLogger("vwap_wave.reconciler")

    def reconcile(self, book: PositionBook, adopt_orphans: bool = True) -> ReconcileReport:
        report = ReconcileReport()

        broker_positions = self._alpaca.get_positions()
        broker_by_symbol: dict[str, dict] = {
            p["symbol"]: p for p in broker_positions
        }

        # 1. Closed: in book, not in broker.
        for symbol in list(book.symbols()):
            if symbol not in broker_by_symbol:
                pos = book.close(symbol)
                report.closed.append(symbol)
                self._log.info(
                    "RECONCILE_CLOSED symbol=%s adopted=%s setup=%s",
                    symbol,
                    getattr(pos, "adopted", "?"),
                    getattr(pos, "setup", "?"),
                )

        # 2. Drift: in both, qty differs (log only).
        for symbol, broker_pos in broker_by_symbol.items():
            local_pos = book.get(symbol)
            if local_pos is None:
                continue
            broker_qty = abs(float(broker_pos["qty"]))
            if abs(local_pos.qty - broker_qty) > _QTY_EPS:
                report.drift.append((symbol, local_pos.qty, broker_qty))
                self._log.warning(
                    "RECONCILE_DRIFT symbol=%s book_qty=%s broker_qty=%s",
                    symbol, local_pos.qty, broker_qty,
                )

        # 3. Orphans: in broker, not in book → adopt by asset class.
        orphan_equity_symbols: list[str] = []
        orphan_crypto_records: list[dict] = []

        if adopt_orphans:
            for symbol, broker_pos in broker_by_symbol.items():
                if book.get(symbol) is not None:
                    continue
                ac = _normalize_asset_class(broker_pos.get("asset_class"))
                if ac == "equity":
                    orphan_equity_symbols.append(symbol)
                elif ac == "crypto":
                    orphan_crypto_records.append(broker_pos)
                else:
                    self._log.warning(
                        "RECONCILE_UNKNOWN_ASSET_CLASS symbol=%s class=%s",
                        symbol, broker_pos.get("asset_class"),
                    )

            # 3a. Equity orphans: one batched list_orders call to recover brackets.
            bracket_index: dict[str, dict] = {}
            if orphan_equity_symbols:
                try:
                    open_orders = self._alpaca.list_orders(
                        status="open",
                        symbols=orphan_equity_symbols,
                        nested=True,
                    )
                    bracket_index = _index_bracket_children(open_orders)
                except Exception as exc:
                    self._log.error(
                        "RECONCILE_LIST_ORDERS_FAILED — adopting orphans without bracket data: %s",
                        exc, exc_info=True,
                    )
                    bracket_index = {}

            for symbol in orphan_equity_symbols:
                broker_pos = broker_by_symbol[symbol]
                legs = bracket_index.get(symbol, {})
                stop_leg = legs.get("stop")
                target_leg = legs.get("target")
                stop_px = float(stop_leg["stop_price"]) if stop_leg else None
                target_px = (float(target_leg["limit_price"])
                             if target_leg else None)
                stop_order_id = stop_leg["id"] if stop_leg else None
                if stop_leg is None and target_leg is None:
                    report.equity_no_bracket.append(symbol)
                    self._log.warning(
                        "RECONCILE_EQUITY_NO_BRACKET symbol=%s qty=%s entry=%s",
                        symbol, broker_pos["qty"],
                        broker_pos["avg_entry_price"],
                    )
                pos = OpenPosition(
                    symbol=symbol,
                    setup="adopted",
                    side=_normalize_side(broker_pos["side"]),
                    qty=abs(float(broker_pos["qty"])),
                    entry_px=float(broker_pos["avg_entry_price"]),
                    stop_px=stop_px,
                    target_px=target_px,
                    opened_at=datetime.now(timezone.utc),
                    order_id="",
                    stop_order_id=stop_order_id,
                    initial_stop_px=stop_px,
                    adopted=True,
                )
                book.add(pos)
                report.adopted_equity.append(symbol)
                self._log.info(
                    "RECONCILE_ADOPTED_EQUITY symbol=%s side=%s qty=%s entry=%s "
                    "stop=%s target=%s stop_leg=%s",
                    symbol, pos.side, pos.qty, pos.entry_px,
                    pos.stop_px, pos.target_px, pos.stop_order_id,
                )

            # 3b. Crypto orphans: naked, loud warning.
            for broker_pos in orphan_crypto_records:
                symbol = broker_pos["symbol"]
                pos = OpenPosition(
                    symbol=symbol,
                    setup="adopted",
                    side=_normalize_side(broker_pos["side"]),
                    qty=abs(float(broker_pos["qty"])),
                    entry_px=float(broker_pos["avg_entry_price"]),
                    stop_px=None,
                    target_px=None,
                    opened_at=datetime.now(timezone.utc),
                    order_id="",
                    stop_order_id=None,
                    initial_stop_px=None,
                    adopted=True,
                )
                book.add(pos)
                report.adopted_crypto.append(symbol)
                self._log.warning(
                    "RECONCILE_ADOPTED_CRYPTO_NO_STOP symbol=%s side=%s qty=%s entry=%s",
                    symbol, pos.side, pos.qty, pos.entry_px,
                )


        # 4. Recurring naked-crypto warning (every cycle).
        for pos in book.all():
            if pos.adopted and pos.stop_px is None:
                self._log.warning(
                    "ADOPTED_CRYPTO_NAKED symbol=%s qty=%s entry=%s — manual close required",
                    pos.symbol, pos.qty, pos.entry_px,
                )

        return report
