"""AlpacaRouter — duck-typed wrapper that fans Alpaca calls across the
per-asset-class accounts (equity, crypto).

After PR #83 each asset class has its own Alpaca account / credentials, so
a single AlpacaClient can no longer see the full broker truth. The reconciler
and any other "global" caller that needs both books goes through this router:

  - get_positions(): concatenate positions from both clients.
  - list_orders(): concatenate orders from both clients (filters propagate;
    a `symbols=[…]` filter is split per asset class so each client only
    sees its own symbols).
  - client_for(broker_pos | symbol): pick the underlying client for a
    write — callers in the reconciler always have a broker position dict
    or an Alpaca-formatted symbol at this point, so routing is unambiguous.

The router only constructs the clients it can resolve credentials for; if
one asset class is missing creds it logs and skips that side. That keeps a
single-account dev setup running on whichever side is configured.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from broker.alpaca_client import AlpacaClient
from broker.credentials import MissingCredentialsError

logger = logging.getLogger(__name__)


def _is_crypto_symbol(symbol: str) -> bool:
    return "/" in symbol


class AlpacaRouter:
    """Routes Alpaca calls to the right per-asset-class client.

    Construct with no args — it builds equity + crypto clients from the
    credentials resolver. If a class is unconfigured, the router still
    works for the other side and logs a warning.
    """

    def __init__(self) -> None:
        self.equity: AlpacaClient | None = self._try_build("equity")
        self.crypto: AlpacaClient | None = self._try_build("crypto")
        if self.equity is None and self.crypto is None:
            raise MissingCredentialsError(
                "AlpacaRouter: neither equity nor crypto credentials configured"
            )

    @staticmethod
    def _try_build(asset_class: str) -> AlpacaClient | None:
        try:
            return AlpacaClient(asset_class=asset_class)
        except MissingCredentialsError as exc:
            logger.warning(
                "ALPACA_ROUTER_MISSING_CREDS asset_class=%s err=%s — "
                "skipping that side", asset_class, exc,
            )
            return None

    def _clients(self) -> list[AlpacaClient]:
        return [c for c in (self.equity, self.crypto) if c is not None]

    # ------------------------------------------------------------------
    # Routing helpers
    # ------------------------------------------------------------------

    def client_for(self, hint: Any) -> AlpacaClient:
        """Resolve the underlying client for a write operation.

        ``hint`` may be:
          - a broker position dict (preferred — uses the authoritative
            ``asset_class`` field returned by Alpaca);
          - a symbol string (falls back to "/" detection).

        Raises MissingCredentialsError if the routed asset class has no
        client configured.
        """
        asset_class: str | None = None
        if isinstance(hint, dict):
            ac = hint.get("asset_class")
            if isinstance(ac, str):
                asset_class = "crypto" if ac == "crypto" else "equity"
            else:
                # Fall back to symbol shape if Alpaca omitted asset_class.
                sym = hint.get("symbol", "")
                asset_class = "crypto" if _is_crypto_symbol(sym) else "equity"
        elif isinstance(hint, str):
            asset_class = "crypto" if _is_crypto_symbol(hint) else "equity"
        else:
            raise TypeError(f"client_for: unsupported hint type {type(hint)!r}")

        client = self.crypto if asset_class == "crypto" else self.equity
        if client is None:
            raise MissingCredentialsError(
                f"AlpacaRouter: no client configured for asset_class={asset_class!r}"
            )
        return client

    # ------------------------------------------------------------------
    # Write paths — route by symbol shape
    # ------------------------------------------------------------------

    def submit_order(self, symbol: str, *args, **kwargs) -> dict:
        return self.client_for(symbol).submit_order(symbol, *args, **kwargs)

    def cancel_order(self, order_id: str) -> bool:
        """Order IDs are per-account and unique, but we don't always know
        which account holds an arbitrary order ID. Try each configured side;
        the first success wins. Re-raises the last error if all fail.
        """
        last_exc: Exception | None = None
        for c in self._clients():
            try:
                return c.cancel_order(order_id)
            except Exception as exc:
                last_exc = exc
                continue
        if last_exc is not None:
            raise last_exc
        return False

    # ------------------------------------------------------------------
    # Read paths — fan out and concatenate
    # ------------------------------------------------------------------

    def get_positions(self) -> list[dict]:
        out: list[dict] = []
        for c in self._clients():
            out.extend(c.get_positions())
        return out

    def list_orders(
        self,
        *,
        status: str = "open",
        symbols: list[str] | None = None,
        nested: bool = True,
        after: datetime | None = None,
    ) -> list[dict]:
        if symbols:
            equity_syms = [s for s in symbols if not _is_crypto_symbol(s)]
            crypto_syms = [s for s in symbols if _is_crypto_symbol(s)]
            out: list[dict] = []
            if equity_syms and self.equity is not None:
                out.extend(self.equity.list_orders(
                    status=status, symbols=equity_syms, nested=nested, after=after,
                ))
            if crypto_syms and self.crypto is not None:
                out.extend(self.crypto.list_orders(
                    status=status, symbols=crypto_syms, nested=nested, after=after,
                ))
            return out

        out = []
        for c in self._clients():
            out.extend(c.list_orders(
                status=status, nested=nested, after=after,
            ))
        return out
