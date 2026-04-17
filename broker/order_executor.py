"""
order_executor.py — High-level order execution layer for the regime_trader system.

Wraps AlpacaClient with risk management checks and provides a clean interface
for the trading engine to interact with the broker.
"""

import logging
import math
from typing import Optional

from broker.alpaca_client import AlpacaClient, BrokerAPIError
from strategies.base_strategy import SignalData


class OrderExecutor:
    """
    Translates trading signals into real broker orders.

    Applies risk manager approval before every order and exposes helpers for
    connectivity verification, position closure, and circuit-breaker-triggered
    emergency unwinds.

    Parameters
    ----------
    alpaca_client : AlpacaClient
        Authenticated Alpaca HTTP client.
    risk_manager :
        A RiskManager instance — must expose ``approve_trade(**kwargs) -> dict``.
    logger : logging.Logger, optional
        If omitted a module-level logger is used (``regime_trader.order_executor``).
    """

    def __init__(
        self,
        alpaca_client: AlpacaClient,
        risk_manager,
        logger: Optional[logging.Logger] = None,
    ):
        self.client = alpaca_client
        self.risk_manager = risk_manager
        self.logger = logger or logging.getLogger("regime_trader.order_executor")

    # ------------------------------------------------------------------
    # Connectivity
    # ------------------------------------------------------------------

    def connectivity_handshake(self, symbol: str = "NVDA") -> bool:
        """
        Verify two-way communication with the broker by placing and immediately
        cancelling a 1-share market order.

        Steps
        -----
        1. Submit a 1-share market buy order for *symbol*.
        2. Confirm the returned order_id is a non-empty string.
        3. Immediately cancel the order.
        4. Log: "HANDSHAKE_OK: order {order_id} placed and cancelled for {symbol}"

        Returns
        -------
        bool
            True on success, False on any failure.
        """
        try:
            order = self.client.submit_order(
                symbol=symbol,
                qty=1,
                side="buy",
                order_type="market",
                time_in_force="day",
            )
            order_id = order.get("id", "")
            if not order_id:
                self.logger.error(
                    "HANDSHAKE_FAILED: no order_id returned for %s", symbol
                )
                return False

            self.client.cancel_order(order_id)
            self.logger.info(
                "HANDSHAKE_OK: order %s placed and cancelled for %s",
                order_id,
                symbol,
            )
            return True

        except Exception as exc:
            self.logger.error(
                "HANDSHAKE_FAILED: unexpected error for %s: %s", symbol, exc
            )
            return False

    # ------------------------------------------------------------------
    # Signal execution
    # ------------------------------------------------------------------

    def execute_signal(
        self,
        ticker: str,
        signal: SignalData,
        current_equity: float,
    ) -> dict:
        """
        Convert a SignalData allocation into a real broker order.

        Steps
        -----
        1. Call ``risk_manager.approve_trade()`` — return rejection dict if not approved.
        2. Fetch current price via ``alpaca_client.get_quote(ticker)``.
        3. Compute shares: ``floor(approved_allocation_pct * current_equity / current_price)``.
        4. If shares > 0: submit a market buy order.
        5. Return the order result dict or a rejection dict.

        Parameters
        ----------
        ticker         : str        — instrument symbol.
        signal         : SignalData — output from the strategy orchestrator.
        current_equity : float      — current portfolio equity in dollars.

        Returns
        -------
        dict
            Order result from the broker, or a rejection dict with key
            ``"approved": False`` and ``"rejection_reason"`` explaining why.
        """
        # Gate 1: Risk manager approval
        approval = self.risk_manager.approve_trade(
            ticker=ticker,
            proposed_allocation_pct=signal.allocation_pct,
            current_positions={},   # caller may pass richer state; default empty
            price_data={},
        )

        if not approval.get("approved", False):
            reason = approval.get("rejection_reason", "Unknown rejection reason")
            self.logger.warning(
                "TRADE_REJECTED: %s — %s", ticker, reason
            )
            return {
                "approved": False,
                "ticker": ticker,
                "rejection_reason": reason,
                "circuit_level": approval.get("circuit_level", 0),
            }

        approved_allocation_pct = approval["approved_allocation_pct"]

        # Gate 2: Fetch current price
        try:
            current_price = self.client.get_quote(ticker)
        except Exception as exc:
            self.logger.error(
                "EXECUTE_SIGNAL_FAILED: could not get quote for %s: %s", ticker, exc
            )
            return {
                "approved": False,
                "ticker": ticker,
                "rejection_reason": f"Failed to fetch quote: {exc}",
                "circuit_level": approval.get("circuit_level", 0),
            }

        # Gate 3: Compute share quantity
        shares = math.floor(approved_allocation_pct * current_equity / current_price)

        if shares <= 0:
            self.logger.info(
                "EXECUTE_SIGNAL_SKIP: %s — computed 0 shares "
                "(allocation_pct=%.4f, equity=%.2f, price=%.2f)",
                ticker,
                approved_allocation_pct,
                current_equity,
                current_price,
            )
            return {
                "approved": True,
                "ticker": ticker,
                "shares": 0,
                "rejection_reason": "Computed share quantity is 0",
                "circuit_level": approval.get("circuit_level", 0),
            }

        # Submit order
        try:
            order = self.client.submit_order(
                symbol=ticker,
                qty=shares,
                side="buy",
                order_type="market",
                time_in_force="day",
            )
            self.logger.info(
                "ORDER_SUBMITTED: %s qty=%d order_id=%s",
                ticker,
                shares,
                order.get("id", "unknown"),
            )
            return order
        except Exception as exc:
            self.logger.error(
                "ORDER_FAILED: %s qty=%d — %s", ticker, shares, exc
            )
            return {
                "approved": True,
                "ticker": ticker,
                "shares": shares,
                "rejection_reason": f"Order submission failed: {exc}",
                "circuit_level": approval.get("circuit_level", 0),
            }

    # ------------------------------------------------------------------
    # Position closure
    # ------------------------------------------------------------------

    def close_position(self, ticker: str) -> dict:
        """
        Market sell all shares of *ticker*.

        Fetches the current held quantity from open positions and submits a
        market sell order for the full size.

        Returns
        -------
        dict
            Order result from the broker, or an error dict.
        """
        try:
            positions = self.client.get_positions()
            qty = 0
            for pos in positions:
                if pos.get("symbol", "").upper() == ticker.upper():
                    qty = int(float(pos.get("qty", 0)))
                    break

            if qty <= 0:
                self.logger.info(
                    "CLOSE_POSITION_SKIP: no open position for %s", ticker
                )
                return {"ticker": ticker, "shares": 0, "message": "No open position"}

            order = self.client.submit_order(
                symbol=ticker,
                qty=qty,
                side="sell",
                order_type="market",
                time_in_force="day",
            )
            self.logger.info(
                "CLOSE_POSITION: %s qty=%d order_id=%s",
                ticker,
                qty,
                order.get("id", "unknown"),
            )
            return order

        except Exception as exc:
            self.logger.error(
                "CLOSE_POSITION_FAILED: %s — %s", ticker, exc
            )
            return {"ticker": ticker, "error": str(exc)}

    def close_all_positions(self) -> list:
        """
        Close all open positions.

        Called by the circuit breaker at Level 2+ (emergency unwind).

        Returns
        -------
        list[dict]
            List of order result dicts (one per closed position).
        """
        results = []
        try:
            positions = self.client.get_positions()
        except Exception as exc:
            self.logger.error(
                "CLOSE_ALL_POSITIONS_FAILED: could not fetch positions — %s", exc
            )
            return [{"error": str(exc)}]

        for pos in positions:
            ticker = pos.get("symbol", "")
            if not ticker:
                continue
            result = self.close_position(ticker)
            results.append(result)

        self.logger.info(
            "CLOSE_ALL_POSITIONS: closed %d position(s)", len(results)
        )
        return results
