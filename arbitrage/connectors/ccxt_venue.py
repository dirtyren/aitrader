"""Thin wrapper around a ccxt.pro exchange producing L1 ``Quote`` rows.

ccxt is imported lazily so the rest of the package works without it.
"""
import time
from typing import Any, Optional

from arbitrage.connectors.base import Quote


def make_exchange(name: str) -> Any:
    """Build a ccxt.pro exchange by id (lazy import)."""
    import ccxt.pro as ccxtpro

    return getattr(ccxtpro, name)({"enableRateLimit": True})


class CcxtVenue:
    def __init__(self, venue: str, exchange: Any):
        self.venue = venue
        self.exchange = exchange

    @staticmethod
    def normalize(symbol: str) -> str:
        """'XBT/USDT' -> 'BTC/USDT', 'BTC-USDT' -> 'BTC/USDT'."""
        parts = symbol.upper().replace("-", "/").split("/")
        parts = ["BTC" if p == "XBT" else p for p in parts]
        return "/".join(parts)

    async def watch_l1(self, symbol: str) -> Optional[Quote]:
        """Await the next order-book update; return its top of book, or None if a side is empty."""
        book = await self.exchange.watch_order_book(symbol)
        bids, asks = book.get("bids"), book.get("asks")
        if not bids or not asks:
            return None
        ts = book.get("timestamp")
        ts_ms = int(ts) if ts is not None else int(time.time() * 1000)
        return Quote(
            venue=self.venue,
            symbol=self.normalize(symbol),
            ts_utc_ms=ts_ms,
            bid=float(bids[0][0]),
            ask=float(asks[0][0]),
        )

    async def close(self) -> None:
        await self.exchange.close()
