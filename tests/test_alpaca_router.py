"""AlpacaRouter — fans Alpaca calls across the per-asset-class accounts.

Covers the router's public shape so callers (reconciler, dashboard
reconciliation tab) can keep treating it as a duck-typed AlpacaClient.
"""
from __future__ import annotations

import os
from unittest.mock import MagicMock

import pytest

os.environ.setdefault("ALPACA_API_KEY", "test")
os.environ.setdefault("ALPACA_SECRET_KEY", "test")

from broker import credentials as creds_mod
from broker.credentials import AlpacaCreds, MissingCredentialsError


@pytest.fixture
def patched_router(monkeypatch):
    """Build an AlpacaRouter whose equity/crypto clients are MagicMocks.

    We bypass real credential resolution and the AlpacaClient HTTP path so
    the test can drive return values per side and assert routing.
    """
    # Make resolve() a no-op so AlpacaClient.__init__ doesn't try to read DB/env.
    monkeypatch.setattr(creds_mod, "resolve", lambda ac: AlpacaCreds(
        asset_class=ac, api_key="AK", secret_key="SK",
        base_url="https://paper-api.alpaca.markets", source="db",
    ))

    from broker.alpaca_router import AlpacaRouter

    router = AlpacaRouter()
    # Replace the real clients with mocks for assertion.
    router.equity = MagicMock(name="equity_client")
    router.equity.asset_class = "equity"
    router.crypto = MagicMock(name="crypto_client")
    router.crypto.asset_class = "crypto"
    return router


def test_get_positions_concatenates_both_sides(patched_router):
    patched_router.equity.get_positions.return_value = [{"symbol": "AAPL"}]
    patched_router.crypto.get_positions.return_value = [{"symbol": "BTC/USD"}]

    out = patched_router.get_positions()
    assert {p["symbol"] for p in out} == {"AAPL", "BTC/USD"}


def test_list_orders_with_symbol_filter_routes_per_class(patched_router):
    patched_router.equity.list_orders.return_value = [{"id": "eq-1"}]
    patched_router.crypto.list_orders.return_value = [{"id": "cr-1"}]

    out = patched_router.list_orders(
        status="open", symbols=["AAPL", "BTC/USD"], nested=False,
    )
    assert {o["id"] for o in out} == {"eq-1", "cr-1"}

    eq_kwargs = patched_router.equity.list_orders.call_args.kwargs
    cr_kwargs = patched_router.crypto.list_orders.call_args.kwargs
    assert eq_kwargs["symbols"] == ["AAPL"]
    assert cr_kwargs["symbols"] == ["BTC/USD"]


def test_list_orders_no_symbols_fans_out(patched_router):
    patched_router.equity.list_orders.return_value = [{"id": "eq-1"}]
    patched_router.crypto.list_orders.return_value = [{"id": "cr-1"}]

    out = patched_router.list_orders(status="closed")
    assert {o["id"] for o in out} == {"eq-1", "cr-1"}


def test_submit_order_routes_by_symbol_shape(patched_router):
    patched_router.equity.submit_order.return_value = {"id": "ord-eq"}
    patched_router.crypto.submit_order.return_value = {"id": "ord-cr"}

    eq = patched_router.submit_order(
        symbol="AAPL", qty=10, side="buy",
        order_type="market", time_in_force="day",
    )
    cr = patched_router.submit_order(
        symbol="BTC/USD", qty=0.5, side="sell",
        order_type="market", time_in_force="gtc",
    )

    assert eq == {"id": "ord-eq"}
    assert cr == {"id": "ord-cr"}
    patched_router.equity.submit_order.assert_called_once()
    patched_router.crypto.submit_order.assert_called_once()


def test_cancel_order_tries_each_side_until_success(patched_router):
    patched_router.equity.cancel_order.side_effect = RuntimeError("not found here")
    patched_router.crypto.cancel_order.return_value = True

    assert patched_router.cancel_order("ord-1") is True
    patched_router.equity.cancel_order.assert_called_once_with("ord-1")
    patched_router.crypto.cancel_order.assert_called_once_with("ord-1")


def test_client_for_uses_broker_pos_asset_class_when_present(patched_router):
    pos = {"symbol": "BTCUSD", "asset_class": "crypto"}
    assert patched_router.client_for(pos) is patched_router.crypto

    pos = {"symbol": "AAPL", "asset_class": "us_equity"}
    # Anything that isn't "crypto" routes to equity.
    assert patched_router.client_for(pos) is patched_router.equity


def test_client_for_falls_back_to_symbol_shape(patched_router):
    # Missing asset_class on the dict → use symbol shape.
    assert patched_router.client_for({"symbol": "BTC/USD"}) is patched_router.crypto
    assert patched_router.client_for({"symbol": "AAPL"}) is patched_router.equity


def test_router_skips_missing_credential_side(monkeypatch):
    """If only one asset class has creds, router still constructs and uses
    the configured side."""
    def _resolve_only_equity(ac):
        if ac == "equity":
            return AlpacaCreds(
                asset_class="equity", api_key="AK", secret_key="SK",
                base_url="https://paper-api.alpaca.markets", source="db",
            )
        raise MissingCredentialsError(f"no creds for {ac}")

    monkeypatch.setattr(creds_mod, "resolve", _resolve_only_equity)

    from broker.alpaca_router import AlpacaRouter

    router = AlpacaRouter()
    assert router.equity is not None
    assert router.crypto is None


def test_router_raises_when_neither_side_configured(monkeypatch):
    monkeypatch.setattr(
        creds_mod, "resolve",
        lambda ac: (_ for _ in ()).throw(MissingCredentialsError(f"no {ac}")),
    )

    from broker.alpaca_router import AlpacaRouter

    with pytest.raises(MissingCredentialsError):
        AlpacaRouter()
