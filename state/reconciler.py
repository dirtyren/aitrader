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
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING
from state.position_book import OpenPosition, PositionBook
from broker.client_order_id import Role, make_client_order_id
from core.asset_class import AssetClassConfig
from notifications import send_position_open_alert
if TYPE_CHECKING:
    from state.mysql_store import MySQLStore


@dataclass
class ReconcileReport:
    closed: list[str] = field(default_factory=list)
    adopted_equity: list[str] = field(default_factory=list)
    adopted_crypto: list[str] = field(default_factory=list)
    drift: list[tuple[str, float, float]] = field(default_factory=list)
    drift_corrected: list[tuple[str, float, float]] = field(default_factory=list)
    drift_ambiguous: list[tuple[str, float, float]] = field(default_factory=list)
    equity_no_bracket: list[str] = field(default_factory=list)
    mysql_closed_gone: list[str] = field(default_factory=list)


_QTY_EPS = 1e-6


def _maybe_crypto_alt(symbol: str) -> str:
    """Return the alternate format for crypto symbols: LINK/USD ↔ LINKUSD.
    
    Helps the reconciler match book positions (which may use LINK/USD) against
    Alpaca broker positions (which return LINKUSD). Identity if no conversion.
    """
    if "/" in symbol:
        # LINK/USD → LINKUSD
        return symbol.replace("/", "")
    # Simple heuristic: if the symbol ends with a known quote currency
    # that would have a "/" book form, produce it.
    crypto_pairs = {"USD", "USDT", "USDC", "EUR", "GBP", "BTC", "ETH"}
    for q in crypto_pairs:
        if symbol.endswith(q) and len(symbol) > len(q):
            base = symbol[: -len(q)]
            if base.isalpha() and 2 <= len(base) <= 5:
                return f"{base}/{q}"
    return symbol


class Reconciler:
    """Reconciles the in-memory PositionBook against Alpaca's /v2/positions.

    Policy (see spec 2026-05-22-broker-position-reconciliation-design.md):
    - MySQL-backed: on startup, positions are loaded from MySQL. Positions in
      MySQL but not on broker were closed server-side (stop/target hit during
      restart) → marked closed in MySQL with reason 'reconciled_gone'.
    - Closed (in book, not in broker): book.close(symbol).
    - Drift (qty differs): log only, no mutation.
    - Orphan (in broker, not in book AND not in MySQL): adopt as monitor-only
      with adopted=True. Equity adoptions recover stop/target/stop_order_id
      from the live bracket children. Crypto adoptions are naked.
    """

    def __init__(self, alpaca, ac_configs: dict | None = None,
                 *, logger: logging.Logger | None = None,
                 mysql_store: "MySQLStore | None" = None,
                 atr_mult_stop: float = 2.0,
                 target_R: float = 1.5,
                 configured_symbols: list[str] | None = None) -> None:
        self._alpaca = alpaca
        self._ac_configs = ac_configs or {}
        self._log = logger or logging.getLogger("vwap_wave.reconciler")
        self._mysql = mysql_store
        self._atr_mult_stop = atr_mult_stop
        self._target_R = target_R
        self._configured_symbols = set(configured_symbols) if configured_symbols is not None else None

    def reconcile(self, book: PositionBook, adopt_orphans: bool = True) -> ReconcileReport:
        report = ReconcileReport()

        broker_positions = self._alpaca.get_positions()
        broker_by_symbol: dict[str, dict] = {
            p["symbol"]: p for p in broker_positions
        }
        broker_symbol_set = set(broker_by_symbol.keys())

        # Expand broker_symbol_set to include crypto alt formats (flat ↔ slash),
        # so MySQL positions stored with slash format (BTC/USD) match broker
        # positions returned as flat format (BTCUSD). Without this, every
        # crypto position gets closed as reconciled_gone on every cycle.
        alt_symbols: set[str] = set()
        for sym in broker_symbol_set:
            alt = _maybe_crypto_alt(sym)
            if alt != sym:
                alt_symbols.add(alt)
        broker_symbol_set |= alt_symbols

        # 0. MySQL: close positions that exist in DB but not on broker.
        #    These are positions that were closed server-side during restart.
        if self._mysql is not None:
            gone = self._mysql.close_positions_not_in_broker(broker_symbol_set)
            report.mysql_closed_gone = gone
            if gone:
                self._log.info(
                    "RECONCILE_MYSQL_CLOSED_GONE count=%d symbols=%s",
                    len(gone), gone,
                )

        # 1. Closed: in book, not in broker (consider crypto alt formats).
        for symbol in list(book.symbols()):
            if symbol not in broker_by_symbol and _maybe_crypto_alt(symbol) not in broker_by_symbol:
                positions = book.get_all(symbol)
                for pos in positions:
                    self._log.info(
                        "RECONCILE_CLOSED symbol=%s adopted=%s setup=%s",
                        symbol,
                        getattr(pos, "adopted", "?"),
                        getattr(pos, "setup", "?"),
                    )
                book.close(symbol)  # closes ALL positions for this symbol
                report.closed.append(symbol)

        # 2. Drift: broker qty aggregates ALL strategies on the account, so a
        #    naive trust-the-broker would corrupt multi-strategy bookkeeping.
        #    Refined rule (MySQL-backed):
        #    - local_sum (sum across ALL strategies in MySQL) == broker_qty
        #        -> no real drift, silent.
        #    - this strategy is the SOLE owner of the symbol
        #        -> trust broker: update_position_qty + sync the in-memory book
        #          (broker is authoritative for a single-strategy account view).
        #    - multiple strategies hold the symbol AND local_sum != broker_qty
        #        -> ambiguous: log loudly, do not mutate.
        for symbol, broker_pos in broker_by_symbol.items():
            positions = book.get_all(symbol)
            if not positions:
                alt = _maybe_crypto_alt(symbol)
                if alt != symbol:
                    positions = book.get_all(alt)
                if not positions:
                    continue
            broker_qty = abs(float(broker_pos["qty"]))

            if self._mysql is not None:
                local_sum = self._mysql.sum_qty_across_strategies(symbol)
                if abs(local_sum - broker_qty) <= _QTY_EPS:
                    continue  # broker total accounted for across strategies
                strategies_holding = self._mysql.count_strategies_holding(symbol)
            else:
                local_sum = sum(p.qty for p in positions)
                strategies_holding = 1

            for local_pos in positions:
                old_qty = local_pos.qty
                if abs(old_qty - broker_qty) <= _QTY_EPS:
                    continue
                report.drift.append((symbol, old_qty, broker_qty))

                if strategies_holding <= 1 and self._mysql is not None:
                    try:
                        self._mysql.update_position_qty(local_pos.symbol, broker_qty)
                        local_pos.qty = broker_qty
                        report.drift_corrected.append(
                            (symbol, old_qty, broker_qty)
                        )
                        self._log.warning(
                            "RECONCILE_DRIFT_CORRECTED symbol=%s setup=%s "
                            "old_qty=%s broker_qty=%s — sole owner, trusting broker",
                            symbol, local_pos.setup, old_qty, broker_qty,
                        )
                    except Exception as exc:
                        self._log.error(
                            "RECONCILE_DRIFT_CORRECT_FAILED symbol=%s setup=%s: %s",
                            symbol, local_pos.setup, exc, exc_info=True,
                        )
                else:
                    report.drift_ambiguous.append(
                        (symbol, old_qty, broker_qty)
                    )
                    self._log.warning(
                        "RECONCILE_DRIFT_AMBIGUOUS symbol=%s setup=%s "
                        "book_qty=%s broker_qty=%s strategies_holding=%d "
                        "local_sum=%s — multi-strategy, manual reconciliation required",
                        symbol, local_pos.setup, old_qty, broker_qty,
                        strategies_holding, local_sum,
                    )

        # 3. Orphans: in broker, not in book → adopt by asset class.
        orphan_equity_symbols: list[str] = []
        orphan_crypto_records: list[dict] = []

        if adopt_orphans:
            for symbol, broker_pos in broker_by_symbol.items():
                if book.get(symbol) is not None:
                    continue
                # Also check if the position exists under a crypto-alternate
                # symbol (e.g. broker LINKUSD vs book LINK/USD) to avoid
                # double-adopting.
                alt = _maybe_crypto_alt(symbol)
                if alt != symbol and book.get(alt) is not None:
                    continue
                if self._configured_symbols is not None:
                    is_configured = (symbol in self._configured_symbols or alt in self._configured_symbols)
                    if not is_configured:
                        self._log.info(
                            "RECONCILE_SKIP_ADOPT_NOT_CONFIGURED symbol=%s — symbol is not in this strategy's configured symbols",
                            symbol,
                        )
                        continue
                if self._mysql is not None:
                    try:
                        if self._mysql.count_strategies_holding(symbol) > 0:
                            self._log.info(
                                "RECONCILE_SKIP_ADOPT_OWNED symbol=%s — position is already owned/open in MySQL under another strategy",
                                symbol,
                            )
                            continue
                    except Exception as exc:
                        self._log.error(
                            "RECONCILE_CHECK_OTHER_OWNERS_FAILED symbol=%s: %s",
                            symbol, exc, exc_info=True,
                        )
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
                    client_order_id=make_client_order_id(
                        self._mysql.strategy_name if self._mysql else "unknown",
                        "adopted", symbol, Role.ADOPTED,
                    ),
                )
                book.add(pos)
                report.adopted_equity.append(symbol)
                self._log.info(
                    "RECONCILE_ADOPTED_EQUITY symbol=%s side=%s qty=%s entry=%s "
                    "stop=%s target=%s stop_leg=%s",
                    symbol, pos.side, pos.qty, pos.entry_px,
                    pos.stop_px, pos.target_px, pos.stop_order_id,
                )
                # Persist adopted equity position to MySQL
                if self._mysql is not None:
                    try:
                        self._mysql.position_opened(pos, "equity")
                    except Exception as exc:
                        self._log.error(
                            "MYSQL_ADOPT_SAVE_FAILED symbol=%s: %s",
                            symbol, exc, exc_info=True,
                        )
                send_position_open_alert(
                    strategy_name=self._mysql.strategy_name if self._mysql else "adopted",
                    symbol=pos.symbol,
                    side=pos.side,
                    qty=pos.qty,
                    entry_px=pos.entry_px,
                    stop_px=pos.stop_px,
                    target_px=pos.target_px,
                    setup_name=pos.setup,
                    asset_class="equity",
                    adopted=True,
                )

            # 3b. Crypto orphans: calculate ATR-based stop/target levels.
            for broker_pos in orphan_crypto_records:
                symbol = broker_pos["symbol"]
                side = _normalize_side(broker_pos["side"])
                qty = abs(float(broker_pos["qty"]))
                entry_px = float(broker_pos["avg_entry_price"])
                current_px = float(broker_pos.get("current_price", entry_px))

                # Calculate ATR(14) from recent 5Min bars
                stop_px: float | None = None
                target_px: float | None = None
                atr14: float | None = None
                try:
                    alt_symbol = _maybe_crypto_alt(symbol)
                    bars = self._alpaca.get_crypto_bars(
                        alt_symbol, "5Min",
                        start=datetime.now(timezone.utc) - timedelta(hours=4),
                        end=datetime.now(timezone.utc), limit=50,
                    )
                    true_ranges = []
                    for i in range(1, min(20, len(bars))):
                        h, l, pc = bars[i]["h"], bars[i]["l"], bars[i-1]["c"]
                        true_ranges.append(max(h - l, abs(h - pc), abs(l - pc)))
                    if len(true_ranges) >= 14:
                        atr14 = statistics.mean(true_ranges[-14:])
                        stop_dist = self._atr_mult_stop * atr14
                        if side == "long":
                            stop_px = round(current_px - stop_dist, 2)
                            target_px = round(current_px + (self._target_R * stop_dist), 2)
                        else:
                            stop_px = round(current_px + stop_dist, 2)
                            target_px = round(current_px - (self._target_R * stop_dist), 2)
                except Exception as exc:
                    self._log.error(
                        "ADOPT_ATR_CALC_FAILED symbol=%s: %s", symbol, exc,
                    )

                pos = OpenPosition(
                    symbol=symbol,
                    setup="adopted",
                    side=side,
                    qty=qty,
                    entry_px=entry_px,
                    stop_px=stop_px,
                    target_px=target_px,
                    opened_at=datetime.now(timezone.utc),
                    order_id="",
                    stop_order_id=None,
                    initial_stop_px=stop_px,
                    adopted=True,
                    client_order_id=make_client_order_id(
                        self._mysql.strategy_name if self._mysql else "unknown",
                        "adopted", symbol, Role.ADOPTED,
                    ),
                )
                book.add(pos)
                report.adopted_crypto.append(symbol)
                if stop_px is not None:
                    self._log.info(
                        "ADOPTED_CRYPTO_WITH_STOP symbol=%s side=%s qty=%s entry=%.4f "
                        "stop=%.4f target=%.4f atr14=%.4f stop_dist=%.4f",
                        symbol, side, qty, entry_px,
                        stop_px, target_px, atr14 or 0, atr14 or 0,
                    )
                else:
                    self._log.warning(
                        "RECONCILE_ADOPTED_CRYPTO_NO_STOP symbol=%s side=%s qty=%s entry=%s",
                        symbol, side, qty, entry_px,
                    )
                # Persist adopted crypto position to MySQL
                if self._mysql is not None:
                    try:
                        self._mysql.position_opened(pos, "crypto")
                    except Exception as exc:
                        self._log.error(
                            "MYSQL_ADOPT_SAVE_FAILED symbol=%s: %s",
                            symbol, exc, exc_info=True,
                        )
                send_position_open_alert(
                    strategy_name=self._mysql.strategy_name if self._mysql else "adopted",
                    symbol=pos.symbol,
                    side=pos.side,
                    qty=pos.qty,
                    entry_px=pos.entry_px,
                    stop_px=pos.stop_px,
                    target_px=pos.target_px,
                    setup_name=pos.setup,
                    asset_class="crypto",
                    adopted=True,
                )

        # 4. Recurring naked-crypto warning (every cycle).
        # Downgrade to INFO for positions that ARE persisted in MySQL — they'll
        # survive restarts now. WARNING only for truly un-tracked naked positions.
        for pos in book.all():
            if pos.adopted and pos.stop_px is None:
                if self._mysql is not None:
                    self._log.info(
                        "ADOPTED_CRYPTO_NAKED_TRACKED symbol=%s qty=%s entry=%s — tracked in MySQL, managed by engine",
                        pos.symbol, pos.qty, pos.entry_px,
                    )
                else:
                    self._log.warning(
                        "ADOPTED_CRYPTO_NAKED symbol=%s qty=%s entry=%s — manual close required",
                        pos.symbol, pos.qty, pos.entry_px,
                    )

        return report
