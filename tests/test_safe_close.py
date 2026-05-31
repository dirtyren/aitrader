"""Unit tests for broker.safe_close.submit_close_with_drift_recovery."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from broker.alpaca_client import InsufficientBuyingPowerError
from broker.safe_close import (
    DEFAULT_DRIFT_MARGIN,
    submit_close_with_drift_recovery,
)


def _ok(order_id: str = "ord-1") -> dict:
    return {"id": order_id}


def test_crypto_first_submit_shrinks_by_margin():
    """Crypto closes shave the fee-drift margin off the requested qty so a
    book/broker drift the size of accumulated fees doesn't trigger a 403."""
    client = MagicMock()
    client.submit_order.return_value = _ok("ord-1")

    out = submit_close_with_drift_recovery(
        client=client, symbol="BTC/USD", qty=1.0, side="sell",
        client_order_id="coid-1", asset_class="crypto",
    )

    assert out == {"id": "ord-1"}
    assert client.submit_order.call_count == 1
    submitted = client.submit_order.call_args.kwargs["qty"]
    assert submitted == pytest.approx(1.0 * (1 - DEFAULT_DRIFT_MARGIN))
    assert submitted < 1.0


def test_equity_first_submit_does_not_shrink():
    """Equity fees come off the cash side; book never exceeds broker.
    Shaving the margin would round whole shares off an integer request,
    so equity passes through unchanged."""
    client = MagicMock()
    client.submit_order.return_value = _ok()

    submit_close_with_drift_recovery(
        client=client, symbol="AAPL", qty=10, side="sell",
        client_order_id="coid", asset_class="equity",
    )

    submitted = client.submit_order.call_args.kwargs["qty"]
    assert submitted == 10


def test_pepe_dust_drift_passes_on_first_submit():
    """Reproduces the production failure: requested 25965312924.201588,
    available 25965312924.201586171 (1.7e-6 unit drift on ~26B units).
    With the margin applied, first submit is below available."""
    client = MagicMock()
    client.submit_order.return_value = _ok()

    requested = 25965312924.201588
    available = 25965312924.201586171

    submit_close_with_drift_recovery(
        client=client, symbol="PEPE/USD", qty=requested, side="sell",
        client_order_id="coid", asset_class="crypto",
    )

    assert client.submit_order.call_count == 1
    submitted = client.submit_order.call_args.kwargs["qty"]
    assert submitted < available


def test_insufficient_balance_falls_back_to_broker_truth():
    """If the first submit still rejects, the helper pulls broker truth and
    retries at broker_qty * (1 - margin)."""
    client = MagicMock()
    client.submit_order.side_effect = [
        InsufficientBuyingPowerError(
            403, "insufficient balance for SOL "
                 "(requested: 1233.09, available: 1230.31)",
        ),
        _ok("recovered"),
    ]
    client.get_positions.return_value = [
        {"symbol": "SOLUSD", "qty": "1230.318126774"},
    ]

    out = submit_close_with_drift_recovery(
        client=client, symbol="SOLUSD", qty=1233.09, side="sell",
        client_order_id="coid", asset_class="crypto",
    )

    assert out == {"id": "recovered"}
    assert client.submit_order.call_count == 2
    retry_qty = client.submit_order.call_args_list[1].kwargs["qty"]
    assert retry_qty == pytest.approx(1230.318126774 * (1 - DEFAULT_DRIFT_MARGIN))


def test_no_broker_position_returns_none_no_retry():
    """If the broker shows no position, retry is impossible — return None."""
    client = MagicMock()
    client.submit_order.side_effect = InsufficientBuyingPowerError(
        403, "insufficient balance for SOL",
    )
    client.get_positions.return_value = []

    out = submit_close_with_drift_recovery(
        client=client, symbol="SOLUSD", qty=1233.0, side="sell",
        client_order_id="coid", asset_class="crypto",
    )

    assert out is None
    assert client.submit_order.call_count == 1


def test_non_qty_error_returns_none_no_retry():
    """Errors that aren't qty-mismatches don't trigger the broker-truth
    fallback (would mask real bugs)."""
    client = MagicMock()
    client.submit_order.side_effect = InsufficientBuyingPowerError(
        403, "insufficient day trading buying power",
    )

    out = submit_close_with_drift_recovery(
        client=client, symbol="AAPL", qty=10, side="sell",
        client_order_id="coid", asset_class="equity",
    )

    assert out is None
    assert client.submit_order.call_count == 1
    # Crucially: no fallback get_positions call.
    client.get_positions.assert_not_called()


def test_symbol_lookup_is_slash_insensitive():
    """Helper must match BTC/USD against broker-reported BTCUSD (and vice
    versa) when pulling broker truth on the retry."""
    client = MagicMock()
    client.submit_order.side_effect = [
        InsufficientBuyingPowerError(403, "insufficient balance for BTC"),
        _ok(),
    ]
    client.get_positions.return_value = [
        {"symbol": "BTCUSD", "qty": "0.4"},
    ]

    submit_close_with_drift_recovery(
        client=client, symbol="BTC/USD", qty=0.5, side="sell",
        client_order_id="coid", asset_class="crypto",
    )

    assert client.submit_order.call_count == 2


def test_extra_kwargs_passthrough():
    """extra_submit_kwargs must reach the broker (e.g. extended_hours)."""
    client = MagicMock()
    client.submit_order.return_value = _ok()

    submit_close_with_drift_recovery(
        client=client, symbol="AAPL", qty=10, side="sell",
        client_order_id="coid", asset_class="equity",
        extra_submit_kwargs={"extended_hours": True},
    )

    kwargs = client.submit_order.call_args.kwargs
    assert kwargs["extended_hours"] is True
