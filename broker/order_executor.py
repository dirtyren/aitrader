from __future__ import annotations
import logging
from typing import Optional

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

        pos = OpenPosition(
            symbol=signal.symbol, setup=signal.setup, side=signal.side,
            qty=decision.qty, entry_px=signal.entry, stop_px=signal.stop,
            target_px=signal.target, opened_at=signal.ts,
            order_id=order.get("id", ""),
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
