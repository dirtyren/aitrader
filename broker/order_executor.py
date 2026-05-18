from __future__ import annotations
import logging
from typing import Optional

from core.position_manager import PositionAction
from state.position_book import OpenPosition, PositionBook
from strategies.base_setup import SetupSignal
from risk.manager import RiskDecision

logger = logging.getLogger(__name__)


class OrderExecutor:
    """Translates an approved SetupSignal+RiskDecision into broker orders."""

    def __init__(self, alpaca_client, book: PositionBook,
                 logger: logging.Logger | None = None):
        self.client = alpaca_client
        self.book = book
        self.logger = logger or logging.getLogger("vwap_wave.executor")

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

        alp_side = self._alpaca_side(signal.side)

        try:
            if asset_class == "equity":
                order = self.client.submit_bracket_order(
                    symbol=signal.symbol,
                    qty=decision.qty,
                    side=alp_side,
                    limit_price=signal.entry,
                    stop_loss=signal.stop,
                    take_profit=signal.target,
                    time_in_force="day",
                )
            elif asset_class == "crypto":
                # Crypto: market entry + engine-managed virtual stop/target
                order = self.client.submit_order(
                    symbol=signal.symbol,
                    qty=decision.qty,
                    side=alp_side,
                    order_type="market",
                    time_in_force="gtc",
                )
            else:
                raise ValueError(f"Unknown asset_class: {asset_class}")
        except Exception as exc:
            self.logger.error("ORDER_SUBMIT_FAILED symbol=%s error=%s",
                              signal.symbol, exc, exc_info=True)
            return None

        stop_order_id = self._extract_stop_leg_id(order) if asset_class == "equity" else None
        pos = OpenPosition(
            symbol=signal.symbol, setup=signal.setup, side=signal.side,
            qty=decision.qty, entry_px=signal.entry, stop_px=signal.stop,
            target_px=signal.target, opened_at=signal.ts,
            order_id=order.get("id", ""),
            stop_order_id=stop_order_id,
            initial_stop_px=signal.stop,
        )
        self.book.add(pos)
        self.logger.info("ORDER_SUBMITTED setup=%s symbol=%s side=%s qty=%s "
                         "entry=%.4f stop=%.4f target=%.4f order_id=%s",
                         signal.setup, signal.symbol, signal.side, decision.qty,
                         signal.entry, signal.stop, signal.target, order.get("id"))
        return pos

    def close_position(self, symbol: str, side: str, qty: float) -> dict | None:
        """Submit a market close order. Used for virtual stops / time stops."""
        try:
            return self.client.submit_order(
                symbol=symbol, qty=qty,
                side="sell" if side == "long" else "buy",
                order_type="market", time_in_force="gtc",
            )
        except Exception as exc:
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
                    self.close_position(a.symbol, a.side, a.qty)
                    self.logger.info("VIRTUAL_EXIT symbol=%s kind=%s price=%.4f qty=%s",
                                     a.symbol, a.kind, a.price, a.qty)
                    continue

            self.logger.warning("UNHANDLED_ACTION symbol=%s kind=%s asset_class=%s",
                                a.symbol, a.kind, asset_class)

    def _move_equity_stop_to_breakeven(self, a: PositionAction) -> None:
        pos = self.book.get(a.symbol)
        stop_leg = pos.stop_order_id if pos else None
        if not stop_leg:
            self.logger.warning("BREAKEVEN_NO_STOP_LEG symbol=%s — skipping replace", a.symbol)
            return
        try:
            self.client.replace_order(stop_leg, stop_price=a.price)
            self.logger.info("BREAKEVEN_REPLACED symbol=%s stop_leg=%s new_stop=%.4f",
                             a.symbol, stop_leg, a.price)
        except Exception as exc:
            self.logger.error("BREAKEVEN_REPLACE_FAILED symbol=%s stop_leg=%s error=%s",
                              a.symbol, stop_leg, exc, exc_info=True)
