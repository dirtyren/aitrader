from datetime import datetime, timezone
from unittest.mock import MagicMock
from broker.order_executor import OrderExecutor
from strategies.base_setup import SetupSignal
from state.position_book import PositionBook
from risk.manager import RiskDecision


def _signal(symbol="AAPL", side="long"):
    return SetupSignal(setup="price_discovery", symbol=symbol, side=side,
                       entry=100, stop=99, target=102, atr=1.0, level=100,
                       ts=datetime(2026, 5, 14, 14, 0, tzinfo=timezone.utc))


def test_submit_equity_uses_bracket_order():
    client = MagicMock()
    client.submit_bracket_order.return_value = {"id": "ord-1"}
    book = PositionBook()
    ex = OrderExecutor(client, book, logger=MagicMock())
    decision = RiskDecision(approved=True, qty=10, notional=1000)
    pos = ex.submit(_signal(), decision, asset_class="equity")
    assert pos is not None
    assert pos.symbol == "AAPL"
    assert client.submit_bracket_order.called
    payload = client.submit_bracket_order.call_args.kwargs
    assert payload["side"] == "buy"
    assert payload["symbol"] == "AAPL"
    assert payload["stop_loss"] == 99
    assert payload["take_profit"] == 102


def test_submit_crypto_uses_market_order_and_virtual_stop():
    client = MagicMock()
    client.submit_order.return_value = {"id": "ord-2"}
    book = PositionBook()
    ex = OrderExecutor(client, book, logger=MagicMock())
    decision = RiskDecision(approved=True, qty=0.1, notional=5000)
    sig = _signal(symbol="BTC/USD", side="long")
    pos = ex.submit(sig, decision, asset_class="crypto")
    assert pos is not None
    client.submit_order.assert_called_once()
    payload = client.submit_order.call_args.kwargs
    assert payload["symbol"] == "BTC/USD"
    assert payload["order_type"] == "market"
    # Virtual stop is tracked in the book
    assert book.get("BTC/USD").stop_px == 99


def test_submit_returns_none_when_rejected():
    client = MagicMock()
    book = PositionBook()
    ex = OrderExecutor(client, book, logger=MagicMock())
    decision = RiskDecision.reject("denied")
    pos = ex.submit(_signal(), decision, asset_class="equity")
    assert pos is None
    client.submit_bracket_order.assert_not_called()
    client.submit_order.assert_not_called()


def test_submit_equity_captures_stop_leg_id():
    client = MagicMock()
    client.submit_bracket_order.return_value = {
        "id": "parent-1",
        "legs": [
            {"id": "tp-1", "type": "limit", "limit_price": 102},
            {"id": "sl-1", "type": "stop", "stop_price": 99},
        ],
    }
    book = PositionBook()
    ex = OrderExecutor(client, book, logger=MagicMock())
    decision = RiskDecision(approved=True, qty=10, notional=1000)
    pos = ex.submit(_signal(), decision, asset_class="equity")
    assert pos.stop_order_id == "sl-1"
    assert pos.order_id == "parent-1"


def test_submit_equity_no_legs_keeps_stop_order_id_none():
    client = MagicMock()
    client.submit_bracket_order.return_value = {"id": "parent-2"}   # paper sometimes omits legs
    book = PositionBook()
    ex = OrderExecutor(client, book, logger=MagicMock())
    decision = RiskDecision(approved=True, qty=10, notional=1000)
    pos = ex.submit(_signal(), decision, asset_class="equity")
    assert pos.stop_order_id is None
