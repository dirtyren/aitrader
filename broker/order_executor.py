"""
order_executor.py — High-level order execution layer for the regime_trader system.

Wraps AlpacaClient with risk management checks and provides a clean interface
for the trading engine to interact with the broker.
"""

from __future__ import annotations

import logging
import math
from typing import TYPE_CHECKING, Optional

from broker.alpaca_client import AlpacaClient, BrokerAPIError, OrderRejectedError, AuthenticationError, RateLimitError
from strategies.base_strategy import SignalData

if TYPE_CHECKING:
    from core.portfolio import Portfolio


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
        Verify two-way communication with the broker by querying the account
        endpoint and fetching a quote.

        Steps
        -----
        1. Call ``get_account()`` to verify authentication and connectivity.
        2. Call ``get_quote(symbol)`` to verify market data access.
        3. Log: "HANDSHAKE_OK: account verified, quote fetched for {symbol}"

        Returns
        -------
        bool
            True on success, False on any failure.
        """
        try:
            account = self.client.get_account()
            if not account.get("id"):
                self.logger.error("HANDSHAKE_FAILED: account response missing id")
                return False

            price = self.client.get_quote(symbol)
            if price <= 0:
                self.logger.error(
                    "HANDSHAKE_FAILED: invalid quote price %.4f for %s", price, symbol
                )
                return False

            self.logger.info(
                "HANDSHAKE_OK: account verified, quote fetched for %s (price=%.2f)",
                symbol, price,
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
        daily_pnl_pct: float = 0.0,
        current_positions: dict | None = None,
        price_data: dict | None = None,
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
            current_positions=current_positions or {},
            price_data=price_data or {},
            daily_pnl_pct=daily_pnl_pct,
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
        except OrderRejectedError as exc:
            self.logger.warning("ORDER_REJECTED_BY_BROKER: %s — %s", ticker, exc)
            result = {
                "approved": True,
                "ticker": ticker,
                "shares": shares,
                "circuit_level": approval.get("circuit_level", 0),
            }
            return {**result, "rejection_reason": str(exc)}
        except (AuthenticationError, RateLimitError) as exc:
            self.logger.critical("BROKER_CRITICAL_ERROR: %s — %s", ticker, exc)
            result = {
                "approved": True,
                "ticker": ticker,
                "shares": shares,
                "circuit_level": approval.get("circuit_level", 0),
            }
            return {**result, "rejection_reason": f"CRITICAL: {exc}"}
        except Exception as exc:
            self.logger.error("ORDER_FAILED: %s — %s", ticker, exc)
            result = {
                "approved": True,
                "ticker": ticker,
                "shares": shares,
                "circuit_level": approval.get("circuit_level", 0),
            }
            return {**result, "rejection_reason": str(exc)}

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

    # ------------------------------------------------------------------
    # Portfolio rebalancing
    # ------------------------------------------------------------------

    def rebalance_portfolio(
        self,
        portfolio: "Portfolio",
        current_positions: dict[str, float],
        signal_map: dict[str, "SignalData"],
        current_equity: float,
        daily_pnl_pct: float = 0.0,
    ) -> list[dict]:
        """
        Rebalance portfolio to regime-adjusted target weights.

        Steps:
        1. Compute regime_adjusted_targets from portfolio + signal_map
        2. For each asset in portfolio.tickers:
           a. target_dollar = target_weight * current_equity
           b. current_dollar = current_positions.get(ticker, 0.0)
           c. delta_dollar = target_dollar - current_dollar
           d. If abs(delta_dollar) < 0.01 * current_equity: skip (below 1% threshold)
           e. If delta_dollar > 0: BUY — call execute_signal with approved allocation
           f. If delta_dollar < 0: SELL — call close_position or partial sell
        3. Collect and return list of order result dicts (one per trade attempted)

        For buys: convert delta_dollar to allocation_pct = delta_dollar / current_equity,
        create a SignalData with that allocation_pct and pass to execute_signal.

        For sells: use alpaca_client.submit_order(ticker, qty, "sell") directly
        where qty = floor(abs(delta_dollar) / current_price).
        Skip if qty == 0.

        Log summary: "REBALANCE: {n_buys} buys, {n_sells} sells, {n_skipped} skipped"
        """
        target_weights: dict[str, float] = portfolio.regime_adjusted_targets(signal_map)
        threshold = 0.01 * current_equity

        results: list[dict] = []
        n_buys = 0
        n_sells = 0
        n_skipped = 0

        for ticker in portfolio.tickers:
            target_weight = target_weights.get(ticker, 0.0)
            target_dollar = target_weight * current_equity
            current_dollar = current_positions.get(ticker, 0.0)
            delta_dollar = target_dollar - current_dollar

            if abs(delta_dollar) < threshold:
                n_skipped += 1
                continue

            if delta_dollar > 0:
                # BUY: build a minimal SignalData carrying the required allocation
                allocation_pct = delta_dollar / current_equity
                buy_signal = SignalData(
                    regime=signal_map[ticker].regime if ticker in signal_map else "Unknown",
                    confidence=signal_map[ticker].confidence if ticker in signal_map else 0.0,
                    allocation_pct=allocation_pct,
                    leverage=signal_map[ticker].leverage if ticker in signal_map else 1.0,
                    stable=signal_map[ticker].stable if ticker in signal_map else False,
                    high_uncertainty=signal_map[ticker].high_uncertainty if ticker in signal_map else True,
                )
                result = self.execute_signal(
                    ticker, buy_signal, current_equity,
                    daily_pnl_pct=daily_pnl_pct,
                    current_positions=current_positions,
                )
                results.append(result)
                n_buys += 1
            else:
                # SELL: check circuit breaker then submit partial sell
                sell_approval = self.risk_manager.approve_trade(
                    ticker=ticker,
                    proposed_allocation_pct=abs(delta_dollar) / current_equity,
                    current_positions=current_positions,
                    price_data={},
                    daily_pnl_pct=daily_pnl_pct,
                )
                if sell_approval.get("circuit_level", 0) >= 2 and sell_approval.get("approved", True) is False:
                    self.logger.warning(
                        "REBALANCE_SELL_BLOCKED: %s — circuit breaker level %d",
                        ticker, sell_approval.get("circuit_level", 0),
                    )
                    results.append({"ticker": ticker, "approved": False,
                                    "rejection_reason": "Circuit breaker blocked sell"})
                    n_skipped += 1
                    continue

                try:
                    current_price = self.client.get_quote(ticker)
                    qty = math.floor(abs(delta_dollar) / current_price)
                    if qty == 0:
                        n_skipped += 1
                        continue
                    order = self.client.submit_order(
                        symbol=ticker,
                        qty=qty,
                        side="sell",
                        order_type="market",
                        time_in_force="day",
                    )
                    self.logger.info(
                        "REBALANCE_SELL: %s qty=%d order_id=%s",
                        ticker,
                        qty,
                        order.get("id", "unknown"),
                    )
                    results.append(order)
                    n_sells += 1
                except Exception as exc:
                    self.logger.error(
                        "REBALANCE_SELL_FAILED: %s — %s", ticker, exc
                    )
                    results.append({"ticker": ticker, "error": str(exc)})
                    n_sells += 1

        self.logger.info(
            "REBALANCE: %d buys, %d sells, %d skipped", n_buys, n_sells, n_skipped
        )
        return results
