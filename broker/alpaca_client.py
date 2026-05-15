"""
alpaca_client.py — Raw HTTP client for the Alpaca Markets REST API v2.

Authentication is loaded exclusively from environment variables.
Never hardcode credentials.
"""

import os
import random
import time
import logging

import requests
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

_DATA_BASE_URL = "https://data.alpaca.markets"

# ---------------------------------------------------------------------------
# Custom exceptions
# ---------------------------------------------------------------------------


class AuthenticationError(Exception):
    """Raised when Alpaca returns 401 Unauthorized."""


class RateLimitError(Exception):
    """Raised after exhausting retries on 429 Too Many Requests."""


class OrderRejectedError(Exception):
    """Raised when Alpaca returns 422 Unprocessable Entity."""


class BrokerAPIError(Exception):
    """Raised for any unexpected 4xx/5xx response."""

    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        self.message = message
        super().__init__(f"BrokerAPIError({status_code}): {message}")


# ---------------------------------------------------------------------------
# AlpacaClient
# ---------------------------------------------------------------------------


class AlpacaClient:
    """
    Thin HTTP wrapper around Alpaca Markets REST API v2.

    Credentials are read from environment variables at construction time:
        ALPACA_API_KEY    — required
        ALPACA_SECRET_KEY — required
        ALPACA_BASE_URL   — optional, defaults to paper trading endpoint
    """

    _MAX_RETRIES = 5

    def __init__(self):
        load_dotenv()
        self.api_key = os.environ["ALPACA_API_KEY"]
        self.secret_key = os.environ["ALPACA_SECRET_KEY"]
        self.base_url = os.environ.get(
            "ALPACA_BASE_URL", "https://paper-api.alpaca.markets"
        ).rstrip("/")
        self._session = requests.Session()
        self._session.headers.update(self._get_headers())

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_headers(self) -> dict:
        return {
            "APCA-API-KEY-ID": self.api_key,
            "APCA-API-SECRET-KEY": self.secret_key,
            "Content-Type": "application/json",
        }

    def _request(
        self,
        method: str,
        path: str,
        **kwargs,
    ):
        """
        Dispatch an HTTP request with retry logic for 429 responses.

        Raises
        ------
        AuthenticationError  — on 401
        RateLimitError       — on 429 after _MAX_RETRIES attempts
        OrderRejectedError   — on 422
        BrokerAPIError       — on any other 4xx/5xx
        """
        url = f"{self.base_url}{path}"

        for attempt in range(self._MAX_RETRIES + 1):
            response = self._session.request(method, url, timeout=10, **kwargs)

            if response.status_code == 429:
                if attempt == self._MAX_RETRIES:
                    raise RateLimitError(
                        f"Rate limit exceeded after {self._MAX_RETRIES} retries on {method} {path}"
                    )
                wait = (2 ** attempt) + random.uniform(0, 1)  # jitter up to 1 second
                logger.warning(
                    "Rate limited by Alpaca (attempt %d/%d). Waiting %.2fs before retry.",
                    attempt + 1,
                    self._MAX_RETRIES,
                    wait,
                )
                time.sleep(wait)
                continue

            # All other responses do not retry
            if response.status_code == 401:
                raise AuthenticationError("Invalid API credentials")

            if response.status_code == 422:
                try:
                    body = response.json()
                    message = body.get("message", response.text)
                except Exception:
                    message = response.text
                raise OrderRejectedError(message)

            if response.status_code >= 400:
                try:
                    body = response.json()
                    message = body.get("message", response.text)
                except Exception:
                    message = response.text
                raise BrokerAPIError(response.status_code, message)

            # Success
            return response

        # Should never be reached
        raise RateLimitError(f"Rate limit retry loop exhausted for {method} {path}")

    def _data_request(self, method: str, path: str, **kwargs):
        """Identical retry semantics to _request, but against the data host."""
        url = f"{_DATA_BASE_URL}{path}"
        for attempt in range(self._MAX_RETRIES + 1):
            response = self._session.request(method, url, timeout=10, **kwargs)
            if response.status_code == 429:
                if attempt == self._MAX_RETRIES:
                    raise RateLimitError(
                        f"Rate limit exceeded after {self._MAX_RETRIES} retries on {method} {path}"
                    )
                wait = (2 ** attempt) + random.uniform(0, 1)
                logger.warning("Data rate limited (attempt %d/%d). Waiting %.2fs.",
                               attempt + 1, self._MAX_RETRIES, wait)
                time.sleep(wait)
                continue
            if response.status_code == 401:
                raise AuthenticationError("Invalid API credentials")
            if response.status_code >= 400:
                try:
                    body = response.json()
                    message = body.get("message", response.text)
                except Exception:
                    message = response.text
                raise BrokerAPIError(response.status_code, message)
            return response
        raise RateLimitError(f"Rate limit retry loop exhausted for {method} {path}")

    # ------------------------------------------------------------------
    # Public API endpoints
    # ------------------------------------------------------------------

    def get_account(self) -> dict:
        """GET /v2/account — returns account info dict."""
        response = self._request("GET", "/v2/account")
        return response.json()

    def get_positions(self) -> list:
        """GET /v2/positions — returns list of position dicts."""
        response = self._request("GET", "/v2/positions")
        return response.json()

    def submit_order(
        self,
        symbol: str,
        qty: int,
        side: str,
        order_type: str = "market",
        time_in_force: str = "day",
    ) -> dict:
        """POST /v2/orders — submit a new order and return the order dict."""
        payload = {
            "symbol": symbol,
            "qty": qty,
            "side": side,
            "type": order_type,
            "time_in_force": time_in_force,
        }
        response = self._request("POST", "/v2/orders", json=payload)
        return response.json()

    def get_order(self, order_id: str) -> dict:
        """GET /v2/orders/{order_id} — returns order dict."""
        response = self._request("GET", f"/v2/orders/{order_id}")
        return response.json()

    def cancel_order(self, order_id: str) -> bool:
        """
        DELETE /v2/orders/{order_id} — cancel an open order.

        Returns True on success (HTTP 204).
        """
        response = self._request("DELETE", f"/v2/orders/{order_id}")
        return response.status_code == 204

    def get_quote(self, symbol: str) -> float:
        """
        GET /v2/stocks/{symbol}/quotes/latest — returns the latest ask price as float.

        Falls back to the bid price if ask is zero, then to the last trade price.
        """
        response = self._request(
            "GET",
            f"/v2/stocks/{symbol}/quotes/latest",
        )
        data = response.json()
        # Alpaca wraps the quote inside {"quote": {...}}
        quote = data.get("quote", data)
        try:
            ask_price = float(quote.get("ap") or 0)
        except (TypeError, ValueError):
            ask_price = 0.0
        if ask_price > 0:
            return ask_price
        try:
            bid_price = float(quote.get("bp") or 0)
        except (TypeError, ValueError):
            bid_price = 0.0
        if bid_price > 0:
            return bid_price
        # Last resort: latest trade
        try:
            trade_price = float(quote.get("lp") or 0)
        except (TypeError, ValueError):
            trade_price = 0.0
        if trade_price > 0:
            return trade_price
        raise BrokerAPIError(200, f"Could not determine a valid price for {symbol}")

    def get_stock_bars(self, symbol: str, timeframe: str, start, end, limit: int = 10000) -> list[dict]:
        """GET /v2/stocks/{symbol}/bars — returns list of bar dicts (Alpaca raw shape)."""
        params = {
            "timeframe": timeframe,
            "start": start.isoformat().replace("+00:00", "Z"),
            "end": end.isoformat().replace("+00:00", "Z"),
            "limit": limit,
            "adjustment": "raw",
            "feed": "iex",
        }
        response = self._data_request("GET", f"/v2/stocks/{symbol}/bars", params=params)
        return response.json().get("bars", []) or []

    def get_crypto_bars(self, symbol: str, timeframe: str, start, end, limit: int = 10000) -> list[dict]:
        """GET /v1beta3/crypto/us/bars — returns list of bar dicts for one symbol."""
        params = {
            "symbols": symbol,
            "timeframe": timeframe,
            "start": start.isoformat().replace("+00:00", "Z"),
            "end": end.isoformat().replace("+00:00", "Z"),
            "limit": limit,
        }
        response = self._data_request("GET", "/v1beta3/crypto/us/bars", params=params)
        body = response.json().get("bars", {}) or {}
        return body.get(symbol, []) or []
