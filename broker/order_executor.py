from __future__ import annotations
import logging
from typing import Optional

from broker.alpaca_client import InsufficientBuyingPowerError, OrderRejectedError
from core.position_manager import PositionAction
from state.position_book import OpenPosition, PositionBook
from strategies.base_setup import SetupSignal
from risk.manager import RiskDecision
from notifications import send_position_open_alert
from broker.client_order_id import Role, make_client_order_id

logger = logging.getLogger(__name__)

# Alpaca error fragments that indicate a breakeven replace is benign:
#  - "must be (>=|<=) base_price ± 0.01": stop drifted past current quote
#    between bar-close decision and PATCH; original bracket stop still protects.
#  - "order is not open": bracket child already filled or canceled.
_BENIGN_BREAKEVEN_FRAGMENTS = (
    "must be >= base_price",
    "must be <= base_price",
    "order is not open",
)


class OrderExecutor:
    """Translates an approved SetupSignal+RiskDecision into broker orders."""

    def __init__(self, alpaca_client, book: PositionBook,
                 strategy_name: str,
                 logger: logging.Logger | None = None,
                 mysql_store=None):
        if not strategy_name:
            raise ValueError("OrderExecutor requires a non-empty strategy_name")
        self.client = alpaca_client
        self.book = book
        self.strategy_name = strategy_name
        self.logger = logger or logging.getLogger("vwap_wave.executor")
        self._dtbp_exhausted = False
        self._mysql = mysql_store

    def reset_cycle(self) -> None:
        """Clear per-cycle short-circuit flags. Call at the top of each main-loop tick."""
        self._dtbp_exhausted = False

    @staticmethod
    def _alpaca_side(side: str) -> str:
        return "buy" if side == "long" else "sell"

    @staticmethod
    def _extract_stop_leg_id(bracket_response: dict) -> str | None:
        """Find the stop_loss child id in a bracket order response.

        Alpaca returns children under `legs`; the stop leg has `stop_price` set
        (and typically type "stop" / "stop_limit"). Match by stop_price presence
        for resilience against minor schema variations.
        """
        for leg in bracket_response.get("legs") or []:
            if leg.get("stop_price") is not None or leg.get("type") in ("stop", "stop_limit"):
                return leg.get("id")
        return None

    def submit(self, signal: SetupSignal, decision: RiskDecision,
               asset_class: str) -> Optional[OpenPosition]:
        if not decision.approved:
            self.logger.info("ORDER_REJECTED symbol=%s reason=%s",
                             signal.symbol, decision.reason)
            return None

        if self.book.was_just_exited(signal.symbol):
            self.logger.info("ORDER_SKIPPED_RECENTLY_EXITED symbol=%s setup=%s",
                             signal.symbol, signal.setup)
            return None

        if asset_class == "equity" and self._dtbp_exhausted:
            self.logger.info("ORDER_SKIPPED_DTBP_EXHAUSTED symbol=%s setup=%s",
                             signal.symbol, signal.setup)
            return None

        if asset_class == "crypto" and signal.side == "short":
            self.logger.info("ORDER_SKIPPED_CRYPTO_SHORT symbol=%s setup=%s",
                             signal.symbol, signal.setup)
            return None

        alp_side = self._alpaca_side(signal.side)
        entry_coid = make_client_order_id(
            self.strategy_name, signal.setup, signal.symbol, Role.ENTRY,
        )

        extended_hours = bool(signal.notes.get("extended_hours"))

        try:
            if asset_class == "equity" and extended_hours:
                # Pre-market entry: plain limit with extended_hours=True. The
                # OCO bracket is attached after the regular session opens.
                order = self.client.submit_order(
                    symbol=signal.symbol,
                    qty=decision.qty,
                    side=alp_side,
                    order_type="limit",
                    time_in_force="day",
                    limit_price=signal.entry,
                    client_order_id=entry_coid,
                    extended_hours=True,
                )
            elif asset_class == "equity":
                order = self.client.submit_bracket_order(
                    symbol=signal.symbol,
                    qty=decision.qty,
                    side=alp_side,
                    limit_price=signal.entry,
                    stop_loss=signal.stop,
                    take_profit=signal.target,
                    time_in_force="day",
                    client_order_id=entry_coid,
                )
            elif asset_class == "crypto":
                # Crypto: market entry + engine-managed virtual stop/target
                order = self.client.submit_order(
                    symbol=signal.symbol,
                    qty=decision.qty,
                    side=alp_side,
                    order_type="market",
                    time_in_force="gtc",
                    client_order_id=entry_coid,
                )
            else:
                raise ValueError(f"Unknown asset_class: {asset_class}")
        except InsufficientBuyingPowerError as exc:
            if asset_class == "equity":
                self._dtbp_exhausted = True
            self.logger.warning(
                "ORDER_REJECTED_DTBP symbol=%s setup=%s qty=%s notional=%.2f detail=%s",
                signal.symbol, signal.setup, decision.qty,
                decision.qty * signal.entry, exc.message,
            )
            return None
        except Exception as exc:
            self.logger.error("ORDER_SUBMIT_FAILED symbol=%s error=%s",
                              signal.symbol, exc, exc_info=True)
            return None

        stop_order_id = self._extract_stop_leg_id(order) if asset_class == "equity" else None
        target_order_id = None
        
        # For crypto, place limit TP order immediately
        if asset_class == "crypto" and signal.target is not None:
            try:
                tp_side = "sell" if alp_side == "buy" else "buy"
                tp_coid = make_client_order_id(
                    self.strategy_name, signal.setup, signal.symbol, Role.TARGET,
                )
                tp_order = self.client.submit_order(
                    symbol=signal.symbol,
                    qty=decision.qty,
                    side=tp_side,
                    order_type="limit",
                    limit_price=round(signal.target, 4),
                    time_in_force="gtc",
                    client_order_id=tp_coid,
                )
                target_order_id = tp_order.get("id")
            except Exception as exc:
                self.logger.error("Failed to place crypto TP limit order for %s: %s", signal.symbol, exc)

        pos = OpenPosition(
            symbol=signal.symbol, setup=signal.setup, side=signal.side,
            qty=decision.qty, entry_px=signal.entry, stop_px=signal.stop,
            target_px=signal.target, opened_at=signal.ts,
            order_id=order.get("id", ""),
            stop_order_id=stop_order_id,
            target_order_id=target_order_id,
            initial_stop_px=signal.stop,
            client_order_id=entry_coid,
            pending_oco_attach=extended_hours,
        )
        self.book.add(pos)
        # Persist to MySQL so position metadata survives container restarts
        if self._mysql is not None:
            try:
                self._mysql.position_opened(pos, asset_class)
            except Exception as exc:
                self.logger.error("MYSQL_SAVE_FAILED symbol=%s: %s",
                                  signal.symbol, exc, exc_info=True)
        # Notify Telegram
        send_position_open_alert(
            strategy_name=self.strategy_name,
            symbol=pos.symbol,
            side=pos.side,
            qty=pos.qty,
            entry_px=pos.entry_px,
            stop_px=pos.stop_px,
            target_px=pos.target_px,
            setup_name=pos.setup,
            asset_class=asset_class,
        )
        self.logger.info("ORDER_SUBMITTED setup=%s symbol=%s side=%s qty=%s "
                         "entry=%.4f stop=%.4f target=%.4f order_id=%s",
                         signal.setup, signal.symbol, signal.side, decision.qty,
                         signal.entry, signal.stop, signal.target, order.get("id"))
        return pos

    def close_position(self, symbol: str, side: str, qty: float) -> dict | None:
        """Submit a market close order. Used for virtual stops / time stops.

        The COID uses setup='_unknown' because this path doesn't know which
        setup owned the position. Plan 3's reconciler service supersedes this
        exit path and will use the real setup. The sanitizer strips the leading
        underscore, so the parsed setup is 'unknown'.

        NOTE: The exit COID is sent to Alpaca but is NOT yet persisted to the
        MySQL trades.exit_client_order_id column. The current MySQL close path
        in scheduler/loop.py omits this kwarg. Plan 3's reconciler service will
        back-fill exit_client_order_id by matching Alpaca filled orders to MySQL
        rows via the COID, so this asymmetry is acceptable during rollout.
        """
        exit_coid = make_client_order_id(
            self.strategy_name, "_unknown", symbol, Role.EXIT,
        )
        try:
            return self.client.submit_order(
                symbol=symbol, qty=qty,
                side="sell" if side == "long" else "buy",
                order_type="market", time_in_force="gtc",
                client_order_id=exit_coid,
            )
        except Exception as exc:
            if "insufficient qty" in str(exc).lower() or "not enough" in str(exc).lower() or "qty" in str(exc).lower():
                self.logger.warning("CLOSE_QTY_MISMATCH symbol=%s qty=%s, attempting full position close", symbol, qty)
                try:
                    positions = self.client.get_positions()
                    broker_pos = next((p for p in positions if p["symbol"].replace("/", "") == symbol.replace("/", "")), None)
                    if broker_pos:
                        actual_qty = abs(float(broker_pos["qty"]))
                        if actual_qty > 0:
                            return self.client.submit_order(
                                symbol=symbol, qty=actual_qty,
                                side="sell" if side == "long" else "buy",
                                order_type="market", time_in_force="gtc",
                                client_order_id=exit_coid,
                            )
                except Exception as inner_exc:
                    self.logger.error("CLOSE_FULL_POSITION_FAILED symbol=%s error=%s", symbol, inner_exc)

            self.logger.error("CLOSE_FAILED symbol=%s error=%s", symbol, exc, exc_info=True)
            return None

    def handle_actions(self, actions: list[PositionAction],
                       asset_class: str,
                       parent_order_id: str | None = None) -> None:
        """Dispatch PositionManager actions to the broker.

        Equity bracket: the broker owns stop/target server-side, so we skip
        those. On time_stop we cancel the bracket parent (which kills its OCO
        children) and then market-close the position. Breakeven for equity
        would require replacing the bracket child stop — not yet implemented,
        logged only.

        Crypto: no broker-side stop/target, so every exit kind translates to a
        market close. Breakeven is engine-state only.
        """
        for a in actions:
            if a.kind == "breakeven":
                if asset_class == "equity":
                    self._move_equity_stop_to_breakeven(a)
                else:
                    self.logger.info("BREAKEVEN symbol=%s side=%s entry=%.4f",
                                     a.symbol, a.side, a.price)
                continue

            if asset_class == "equity":
                if a.kind in ("stop", "target"):
                    self.logger.info("BRACKET_EXIT symbol=%s kind=%s price=%.4f",
                                     a.symbol, a.kind, a.price)
                    continue
                if a.kind == "time_stop":
                    if parent_order_id:
                        try:
                            self.client.cancel_order(parent_order_id)
                        except Exception as exc:
                            self.logger.error("CANCEL_FAILED symbol=%s order_id=%s error=%s",
                                              a.symbol, parent_order_id, exc, exc_info=True)
                    self.close_position(a.symbol, a.side, a.qty)
                    self.logger.info("TIME_STOP symbol=%s side=%s qty=%s",
                                     a.symbol, a.side, a.qty)
                    continue

            elif asset_class == "crypto":
                if a.kind in ("stop", "target", "time_stop"):
                    pos = self.book.get(a.symbol, a.setup)
                    if pos and getattr(pos, "target_order_id", None):
                        try:
                            self.client.cancel_order(pos.target_order_id)
                        except Exception as exc:
                            self.logger.error("CANCEL_TP_FAILED symbol=%s order_id=%s error=%s",
                                              a.symbol, pos.target_order_id, exc)
                    
                    is_adopted = pos and getattr(pos, "adopted", False)
                    if a.kind != "target" or is_adopted:
                        # If it wasn't the target filling, or if the position was adopted,
                        # we must submit a market order to close the position on the broker.
                        self.close_position(a.symbol, a.side, a.qty)
                    self.logger.info("VIRTUAL_EXIT symbol=%s kind=%s price=%.4f qty=%s",
                                     a.symbol, a.kind, a.price, a.qty)
                    continue

            self.logger.warning("UNHANDLED_ACTION symbol=%s kind=%s asset_class=%s",
                                a.symbol, a.kind, asset_class)

    def _move_equity_stop_to_breakeven(self, a: PositionAction) -> None:
        pos = self.book.get(a.symbol, a.setup)
        stop_leg = pos.stop_order_id if pos else None
        if not stop_leg:
            self.logger.warning("BREAKEVEN_NO_STOP_LEG symbol=%s — skipping replace", a.symbol)
            return
        try:
            self.client.replace_order(stop_leg, stop_price=a.price)
            self.logger.info("BREAKEVEN_REPLACED symbol=%s stop_leg=%s new_stop=%.4f",
                             a.symbol, stop_leg, a.price)
        except OrderRejectedError as exc:
            msg = str(exc)
            if any(frag in msg for frag in _BENIGN_BREAKEVEN_FRAGMENTS):
                self.logger.warning("BREAKEVEN_SKIPPED symbol=%s stop_leg=%s reason=%s",
                                    a.symbol, stop_leg, msg)
                return
            self.logger.error("BREAKEVEN_REPLACE_FAILED symbol=%s stop_leg=%s error=%s",
                              a.symbol, stop_leg, exc, exc_info=True)
        except Exception as exc:
            self.logger.error("BREAKEVEN_REPLACE_FAILED symbol=%s stop_leg=%s error=%s",
                              a.symbol, stop_leg, exc, exc_info=True)
