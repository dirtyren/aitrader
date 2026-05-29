from __future__ import annotations
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from broker.post_open_attach import attach_brackets_for_premarket_fills
from state.position_book import OpenPosition, PositionBook


_NOW = datetime(2026, 5, 29, 13, 30, tzinfo=timezone.utc)  # 09:30 ET


def _open_position(symbol="AAPL", qty=10, stop=198.0, target=204.0,
                   pending=True) -> OpenPosition:
    return OpenPosition(
        symbol=symbol, setup="gap_and_go", side="long",
        qty=qty, entry_px=200.0, stop_px=stop, target_px=target,
        opened_at=_NOW, order_id="entry-1",
        initial_stop_px=stop,
        pending_oco_attach=pending,
    )


def _book_with(*positions: OpenPosition) -> PositionBook:
    book = PositionBook()
    for p in positions:
        book.add(p)
    return book


def _client_with_position(symbol: str, qty: float):
    client = MagicMock()
    client.get_positions.return_value = [{"symbol": symbol, "qty": str(qty)}]
    client.attach_oco.return_value = {"id": "oco-1"}
    client.submit_order.return_value = {"id": "close-1"}
    return client


def test_no_pending_positions_is_a_no_op():
    book = _book_with(_open_position(pending=False))
    client = MagicMock()
    summary = attach_brackets_for_premarket_fills(
        book, client, strategy_name="gap_and_go", now=_NOW,
    )
    assert summary == {"attached": 0, "failsafe_closed": 0, "skipped": 0}
    client.attach_oco.assert_not_called()
    client.submit_order.assert_not_called()
    client.get_positions.assert_not_called()


def test_happy_path_attaches_oco_and_clears_flag():
    pos = _open_position(qty=10)
    book = _book_with(pos)
    client = _client_with_position("AAPL", 10)

    summary = attach_brackets_for_premarket_fills(
        book, client, strategy_name="gap_and_go", now=_NOW,
    )

    assert summary == {"attached": 1, "failsafe_closed": 0, "skipped": 0}
    client.attach_oco.assert_called_once()
    kwargs = client.attach_oco.call_args.kwargs
    assert kwargs["symbol"] == "AAPL"
    assert kwargs["qty"] == 10
    assert kwargs["side"] == "sell"
    assert kwargs["stop_price"] == 198.0
    assert kwargs["target_price"] == 204.0

    assert pos.pending_oco_attach is False
    assert pos.stop_order_id == "oco-1"
    client.submit_order.assert_not_called()


def test_short_position_attaches_oco_with_buy_side():
    pos = _open_position()
    pos.side = "short"
    book = _book_with(pos)
    client = _client_with_position("AAPL", 10)

    attach_brackets_for_premarket_fills(
        book, client, strategy_name="gap_and_go", now=_NOW,
    )
    assert client.attach_oco.call_args.kwargs["side"] == "buy"


def test_partial_fill_uses_broker_qty_and_updates_book():
    pos = _open_position(qty=10)
    book = _book_with(pos)
    client = _client_with_position("AAPL", 7)  # only 7 of 10 filled

    summary = attach_brackets_for_premarket_fills(
        book, client, strategy_name="gap_and_go", now=_NOW,
    )
    assert summary["attached"] == 1
    assert client.attach_oco.call_args.kwargs["qty"] == 7
    assert pos.qty == 7
    assert pos.pending_oco_attach is False


def test_zero_broker_qty_clears_flag_and_skips_attach():
    """Pre-market limit never filled — no position to bracket."""
    pos = _open_position(qty=10)
    book = _book_with(pos)
    client = MagicMock()
    client.get_positions.return_value = []  # nothing on broker

    summary = attach_brackets_for_premarket_fills(
        book, client, strategy_name="gap_and_go", now=_NOW,
    )
    assert summary == {"attached": 0, "failsafe_closed": 0, "skipped": 1}
    client.attach_oco.assert_not_called()
    client.submit_order.assert_not_called()
    assert pos.pending_oco_attach is False


def test_oco_failure_triggers_failsafe_market_close():
    pos = _open_position(qty=10)
    book = _book_with(pos)
    client = MagicMock()
    client.get_positions.return_value = [{"symbol": "AAPL", "qty": "10"}]
    client.attach_oco.side_effect = RuntimeError("OCO rejected: 422")
    client.submit_order.return_value = {"id": "close-1"}

    summary = attach_brackets_for_premarket_fills(
        book, client, strategy_name="gap_and_go", now=_NOW,
    )
    assert summary == {"attached": 0, "failsafe_closed": 1, "skipped": 0}

    client.submit_order.assert_called_once()
    close_kwargs = client.submit_order.call_args.kwargs
    assert close_kwargs["symbol"] == "AAPL"
    assert close_kwargs["qty"] == 10
    assert close_kwargs["side"] == "sell"
    assert close_kwargs["order_type"] == "market"
    # Flag stays set so a downstream reconciler/operator can investigate why.
    assert pos.pending_oco_attach is True


def test_missing_levels_triggers_failsafe_close():
    """A pending position without stop/target must be flattened, not bracketed."""
    pos = _open_position(qty=5, stop=None, target=None)
    book = _book_with(pos)
    client = MagicMock()
    client.submit_order.return_value = {"id": "close-1"}

    summary = attach_brackets_for_premarket_fills(
        book, client, strategy_name="gap_and_go", now=_NOW,
    )
    assert summary == {"attached": 0, "failsafe_closed": 1, "skipped": 0}
    client.attach_oco.assert_not_called()
    # Failsafe close must still be issued
    client.submit_order.assert_called_once()
    assert client.submit_order.call_args.kwargs["order_type"] == "market"


def test_get_positions_failure_skips_position():
    """If we can't read broker state, do not aggressively close — log and skip."""
    pos = _open_position(qty=10)
    book = _book_with(pos)
    client = MagicMock()
    client.get_positions.side_effect = RuntimeError("network down")

    summary = attach_brackets_for_premarket_fills(
        book, client, strategy_name="gap_and_go", now=_NOW,
    )
    assert summary["attached"] == 0
    assert summary["failsafe_closed"] == 0
    assert summary["skipped"] == 1
    client.attach_oco.assert_not_called()
    client.submit_order.assert_not_called()
    # Flag cleared so the next loop tick does not retry against the same outage —
    # the reconciler is the right component to pick this up.
    assert pos.pending_oco_attach is False


def test_only_pending_positions_are_processed():
    p1 = _open_position(symbol="AAPL", qty=10, pending=True)
    p2 = _open_position(symbol="MSFT", qty=5, pending=False)
    book = _book_with(p1, p2)
    client = MagicMock()
    client.get_positions.return_value = [
        {"symbol": "AAPL", "qty": "10"},
        {"symbol": "MSFT", "qty": "5"},
    ]
    client.attach_oco.return_value = {"id": "oco-1"}

    summary = attach_brackets_for_premarket_fills(
        book, client, strategy_name="gap_and_go", now=_NOW,
    )
    assert summary == {"attached": 1, "failsafe_closed": 0, "skipped": 0}
    client.attach_oco.assert_called_once()
    assert client.attach_oco.call_args.kwargs["symbol"] == "AAPL"
