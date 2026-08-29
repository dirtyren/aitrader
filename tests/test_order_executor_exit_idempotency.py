"""OrderExecutor.handle_actions flips exit_submitted exactly once per
logical exit, both in-memory (book) and persisted (mysql_store).

This is the runtime side of the engine-side fix for the 2026-06-02 COIN
incident. The PositionManager exit gate (test_position_manager_exit_gate)
relies on this flag being flipped by handle_actions; if it isn't, the
gate never closes and the loop returns.
"""
from __future__ import annotations
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from broker.order_executor import OrderExecutor
from core.position_manager import PositionAction
from state.position_book import OpenPosition, PositionBook


def _seed(book: PositionBook, *, exit_submitted: bool = False) -> OpenPosition:
    pos = OpenPosition(
        symbol="COIN", setup="price_discovery", side="short",
        qty=1.0, entry_px=174.07, stop_px=175.31, target_px=171.60,
        opened_at=datetime(2026, 6, 2, 15, 0, tzinfo=timezone.utc),
        order_id="parent-1",
        fill_confirmed=True,
        exit_submitted=exit_submitted,
    )
    book.add(pos)
    return pos


def _make_executor(client, book, mysql_store=None) -> OrderExecutor:
    return OrderExecutor(
        client, book,
        strategy_name="vwap_wave_equity",
        logger=MagicMock(),
        mysql_store=mysql_store,
    )


def test_equity_time_stop_flips_flag_in_book_and_mysql():
    client = MagicMock()
    client.submit_order.return_value = {"id": "close-1"}
    book = PositionBook()
    _seed(book)
    mysql = MagicMock()
    mysql.strategy_id = 42
    ex = _make_executor(client, book, mysql_store=mysql)

    action = PositionAction(
        symbol="COIN", setup="price_discovery", side="short",
        qty=1.0, kind="time_stop", price=174.27,
    )
    ex.handle_actions([action], asset_class="equity",
                      parent_order_id="parent-1")

    assert book.get("COIN", "price_discovery").exit_submitted is True
    mysql.mark_exit_submitted.assert_called_once_with(
        strategy_id=42, symbol="COIN", setup_name="price_discovery",
    )


def test_equity_stop_target_submits_close_and_flips_flag():
    """For equity stop/target, we now submit a market close AND flip the
    flag. The bracket assumption was freezing positions when OCO attach
    failed or day orders expired."""
    client = MagicMock()
    book = PositionBook()
    _seed(book)
    mysql = MagicMock()
    mysql.strategy_id = 42
    ex = _make_executor(client, book, mysql_store=mysql)

    action = PositionAction(
        symbol="COIN", setup="price_discovery", side="short",
        qty=1.0, kind="stop", price=175.31,
    )
    ex.handle_actions([action], asset_class="equity",
                      parent_order_id="parent-1")

    client.submit_order.assert_called_once()  # now submits market close
    assert book.get("COIN", "price_discovery").exit_submitted is True
    mysql.mark_exit_submitted.assert_called_once()


def test_equity_time_stop_cancel_failure_still_flips_and_submits_close():
    """Today's COIN bug: cancel raised because parent was already canceled,
    and the engine kept re-firing because exit_submitted didn't flip.
    With the fix, cancel-fail is logged-and-proceeded."""
    client = MagicMock()
    client.cancel_order.side_effect = Exception(
        "order already in canceled status"
    )
    client.submit_order.return_value = {"id": "close-1"}
    book = PositionBook()
    _seed(book)
    mysql = MagicMock()
    mysql.strategy_id = 42
    ex = _make_executor(client, book, mysql_store=mysql)

    action = PositionAction(
        symbol="COIN", setup="price_discovery", side="short",
        qty=1.0, kind="time_stop", price=174.27,
    )
    ex.handle_actions([action], asset_class="equity",
                      parent_order_id="parent-1")

    assert client.submit_order.called  # close still submitted
    assert book.get("COIN", "price_discovery").exit_submitted is True


def test_close_submission_failure_does_not_flip_flag():
    """If close_position itself raises, leave exit_submitted=False so the
    next cycle retries."""
    client = MagicMock()
    client.submit_order.side_effect = Exception("alpaca 500")
    book = PositionBook()
    _seed(book)
    mysql = MagicMock()
    mysql.strategy_id = 42
    ex = _make_executor(client, book, mysql_store=mysql)

    action = PositionAction(
        symbol="COIN", setup="price_discovery", side="short",
        qty=1.0, kind="time_stop", price=174.27,
    )
    try:
        ex.handle_actions([action], asset_class="equity",
                          parent_order_id="parent-1")
    except Exception:
        pass
    assert book.get("COIN", "price_discovery").exit_submitted is False
    mysql.mark_exit_submitted.assert_not_called()


def test_crypto_time_stop_flips_flag():
    client = MagicMock()
    client.submit_order.return_value = {"id": "close-1"}
    book = PositionBook()
    pos = OpenPosition(
        symbol="BTC/USD", setup="vwap_bands", side="long",
        qty=0.05, entry_px=70000.0, stop_px=69000.0, target_px=71500.0,
        opened_at=datetime(2026, 6, 2, 15, 0, tzinfo=timezone.utc),
        order_id="parent-2", fill_confirmed=True,
    )
    book.add(pos)
    mysql = MagicMock()
    mysql.strategy_id = 99
    ex = OrderExecutor(client, book, strategy_name="vwap_bands_crypto",
                       logger=MagicMock(), mysql_store=mysql)

    action = PositionAction(
        symbol="BTC/USD", setup="vwap_bands", side="long",
        qty=0.05, kind="time_stop", price=69500.0,
    )
    ex.handle_actions([action], asset_class="crypto", parent_order_id=None)

    assert book.get("BTC/USD", "vwap_bands").exit_submitted is True
    mysql.mark_exit_submitted.assert_called_once_with(
        strategy_id=99, symbol="BTC/USD", setup_name="vwap_bands",
    )


def test_crypto_close_submission_failure_does_not_flip_flag():
    """Symmetric to the equity test: a None return from close_position
    (the actual failure mode of submit_close_with_drift_recovery) must
    leave exit_submitted=False so the next cycle retries.
    Without this guard, a transient Alpaca failure on a crypto stop would
    silently lock out the position forever.

    submit_close_with_drift_recovery returns None (rather than raising)
    when submit_order raises a non-qty-rejection error — "alpaca 500"
    does not match "insufficient balance" / "insufficient qty" / "not enough",
    so the first attempt logs SAFE_CLOSE_FAILED and returns None.
    """
    client = MagicMock()
    client.submit_order.side_effect = Exception("alpaca 500")
    book = PositionBook()
    pos = OpenPosition(
        symbol="BTC/USD", setup="vwap_bands", side="long",
        qty=0.05, entry_px=70000.0, stop_px=69000.0, target_px=71500.0,
        opened_at=datetime(2026, 6, 2, 15, 0, tzinfo=timezone.utc),
        order_id="parent-2", fill_confirmed=True,
    )
    book.add(pos)
    mysql = MagicMock()
    mysql.strategy_id = 99
    ex = OrderExecutor(client, book, strategy_name="vwap_bands_crypto",
                       logger=MagicMock(), mysql_store=mysql)

    action = PositionAction(
        symbol="BTC/USD", setup="vwap_bands", side="long",
        qty=0.05, kind="time_stop", price=69500.0,
    )
    try:
        ex.handle_actions([action], asset_class="crypto",
                          parent_order_id=None)
    except Exception:
        pass

    assert book.get("BTC/USD", "vwap_bands").exit_submitted is False
    mysql.mark_exit_submitted.assert_not_called()


def test_mark_exit_submitted_is_public_api():
    """The Opening Drive EOD flatten calls executor.mark_exit_submitted(...) so
    PositionManager stops managing a flattened position. A MagicMock executor
    would accept any spelling — this pins the real method on the real class.
    """
    client = MagicMock()
    book = PositionBook()
    _seed(book)
    mysql = MagicMock()
    mysql.strategy_id = 42
    ex = _make_executor(client, book, mysql_store=mysql)

    ex.mark_exit_submitted("COIN", "price_discovery")

    assert book.get("COIN", "price_discovery").exit_submitted is True
    mysql.mark_exit_submitted.assert_called_once_with(
        strategy_id=42, symbol="COIN", setup_name="price_discovery",
    )
