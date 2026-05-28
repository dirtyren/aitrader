import pytest
from state.reconciler import (
    _normalize_asset_class,
    _normalize_side,
    _index_bracket_children,
)


def test_normalize_asset_class_us_equity():
    assert _normalize_asset_class("us_equity") == "equity"


def test_normalize_asset_class_crypto():
    assert _normalize_asset_class("crypto") == "crypto"


def test_normalize_asset_class_uppercase():
    assert _normalize_asset_class("US_EQUITY") == "equity"


def test_normalize_asset_class_unknown_returns_none():
    assert _normalize_asset_class("forex") is None
    assert _normalize_asset_class("") is None
    assert _normalize_asset_class(None) is None


def test_normalize_side_long():
    assert _normalize_side("long") == "long"


def test_normalize_side_short():
    assert _normalize_side("short") == "short"


def test_normalize_side_uppercase():
    assert _normalize_side("LONG") == "long"


def test_normalize_side_unknown_raises():
    with pytest.raises(ValueError):
        _normalize_side("buy")


def test_index_bracket_children_nested_legs():
    parent = {
        "id": "p1", "symbol": "AAPL", "type": "limit", "side": "buy",
        "legs": [
            {"id": "stop1", "symbol": "AAPL", "type": "stop",
             "stop_price": "99.0", "side": "sell"},
            {"id": "tgt1", "symbol": "AAPL", "type": "limit",
             "limit_price": "102.0", "side": "sell"},
        ],
    }
    idx = _index_bracket_children([parent])
    assert idx["AAPL"]["stop"]["id"] == "stop1"
    assert idx["AAPL"]["target"]["id"] == "tgt1"


def test_index_bracket_children_orphaned_children():
    children = [
        {"id": "stop1", "symbol": "AAPL", "type": "stop_limit",
         "stop_price": "99.0", "parent_id": "p1", "side": "sell"},
        {"id": "tgt1", "symbol": "AAPL", "type": "limit",
         "limit_price": "102.0", "parent_id": "p1", "side": "sell"},
    ]
    idx = _index_bracket_children(children)
    assert idx["AAPL"]["stop"]["id"] == "stop1"
    assert idx["AAPL"]["target"]["id"] == "tgt1"


def test_index_bracket_children_only_stop_present():
    orders = [
        {"id": "stop1", "symbol": "AAPL", "type": "stop",
         "stop_price": "99.0", "side": "sell"},
    ]
    idx = _index_bracket_children(orders)
    assert idx["AAPL"]["stop"]["id"] == "stop1"
    assert idx["AAPL"]["target"] is None


def test_index_bracket_children_empty_input():
    assert _index_bracket_children([]) == {}


def test_index_bracket_children_ignores_unrelated_order_types():
    orders = [
        {"id": "m1", "symbol": "AAPL", "type": "market", "side": "buy"},
    ]
    assert _index_bracket_children(orders) == {}


from datetime import datetime, timezone
import logging
from unittest.mock import MagicMock
from state.position_book import PositionBook, OpenPosition
from state.reconciler import Reconciler, ReconcileReport


def _trader_pos(symbol="AAPL", qty=10, side="long",
                stop=99.0, target=102.0, entry=100.0):
    return OpenPosition(
        symbol=symbol, setup="price_discovery", side=side, qty=qty,
        entry_px=entry, stop_px=stop, target_px=target,
        opened_at=datetime(2026, 5, 14, 14, 0, tzinfo=timezone.utc),
        order_id="o1", stop_order_id="leg1", initial_stop_px=stop,
    )


def _broker_position(symbol="AAPL", qty="10", side="long",
                     entry="100.0", asset_class="us_equity"):
    return {"symbol": symbol, "qty": qty, "side": side,
            "avg_entry_price": entry, "asset_class": asset_class}


def _fake_alpaca(positions=None, orders=None):
    alp = MagicMock()
    alp.get_positions.return_value = positions or []
    alp.list_orders.return_value = orders or []
    return alp


def test_reconcile_empty_book_empty_broker():
    book = PositionBook()
    r = Reconciler(_fake_alpaca(), ac_configs={})
    report = r.reconcile(book)
    assert isinstance(report, ReconcileReport)
    assert report.closed == []
    assert report.adopted_equity == []
    assert report.adopted_crypto == []
    assert report.drift == []
    assert report.equity_no_bracket == []


def test_reconcile_book_matches_broker_no_changes():
    book = PositionBook()
    book.add(_trader_pos("AAPL", qty=10))
    alp = _fake_alpaca(positions=[_broker_position("AAPL", qty="10")])
    r = Reconciler(alp, ac_configs={})
    report = r.reconcile(book)
    assert report.closed == []
    assert report.adopted_equity == []
    assert book.count() == 1
    assert book.get("AAPL").adopted is False


def test_reconcile_closes_position_when_broker_says_gone():
    book = PositionBook()
    book.add(_trader_pos("AAPL"))
    alp = _fake_alpaca(positions=[])
    r = Reconciler(alp, ac_configs={})
    report = r.reconcile(book)
    assert report.closed == ["AAPL"]
    assert book.get("AAPL") is None


def test_reconcile_adopts_equity_with_alive_bracket():
    book = PositionBook()
    bracket_parent = {
        "id": "p1", "symbol": "AAPL", "type": "limit", "side": "buy",
        "legs": [
            {"id": "stop1", "symbol": "AAPL", "type": "stop",
             "stop_price": "99.0", "side": "sell"},
            {"id": "tgt1", "symbol": "AAPL", "type": "limit",
             "limit_price": "102.0", "side": "sell"},
        ],
    }
    alp = _fake_alpaca(
        positions=[_broker_position("AAPL", qty="10", entry="100.0")],
        orders=[bracket_parent],
    )
    r = Reconciler(alp, ac_configs={})
    report = r.reconcile(book)
    assert report.adopted_equity == ["AAPL"]
    pos = book.get("AAPL")
    assert pos is not None
    assert pos.adopted is True
    assert pos.qty == 10.0
    assert pos.side == "long"
    assert pos.entry_px == 100.0
    assert pos.stop_px == 99.0
    assert pos.target_px == 102.0
    assert pos.stop_order_id == "stop1"
    assert pos.initial_stop_px == 99.0


def test_reconcile_adopts_equity_with_orphaned_bracket_children():
    book = PositionBook()
    children = [
        {"id": "stop1", "symbol": "AAPL", "type": "stop_limit",
         "stop_price": "99.0", "parent_id": "p1", "side": "sell"},
        {"id": "tgt1", "symbol": "AAPL", "type": "limit",
         "limit_price": "102.0", "parent_id": "p1", "side": "sell"},
    ]
    alp = _fake_alpaca(
        positions=[_broker_position("AAPL")],
        orders=children,
    )
    r = Reconciler(alp, ac_configs={})
    r.reconcile(book)
    pos = book.get("AAPL")
    assert pos.stop_order_id == "stop1"
    assert pos.stop_px == 99.0
    assert pos.target_px == 102.0


def test_reconcile_adopts_equity_no_bracket():
    book = PositionBook()
    alp = _fake_alpaca(
        positions=[_broker_position("AAPL", qty="10", entry="100.0")],
        orders=[],
    )
    r = Reconciler(alp, ac_configs={})
    report = r.reconcile(book)
    assert report.adopted_equity == ["AAPL"]
    assert report.equity_no_bracket == ["AAPL"]
    pos = book.get("AAPL")
    assert pos.stop_px is None
    assert pos.target_px is None
    assert pos.stop_order_id is None


def test_reconcile_adopts_crypto_no_stop():
    book = PositionBook()
    alp = _fake_alpaca(
        positions=[_broker_position("BTCUSD", qty="0.5",
                                    entry="50000.0", asset_class="crypto")],
        orders=[],
    )
    r = Reconciler(alp, ac_configs={})
    report = r.reconcile(book)
    assert report.adopted_crypto == ["BTCUSD"]
    pos = book.get("BTCUSD")
    assert pos.adopted is True
    assert pos.qty == 0.5
    assert pos.entry_px == 50_000.0
    assert pos.stop_px is None
    assert pos.target_px is None


class _FakeMySQLStore:
    """Test double for the reconciler drift block.

    Mimics only the methods the reconciler calls: sum_qty_across_strategies,
    count_strategies_holding, update_position_qty, plus the no-op hooks for
    close_positions_not_in_broker and position_opened.
    """

    def __init__(self, *, local_sum: float = 0.0, strategies: int = 1):
        self.local_sum = local_sum
        self.strategies = strategies
        self.qty_updates: list[tuple[str, float]] = []
        self.strategy_name = "test"

    def sum_qty_across_strategies(self, symbol):
        return self.local_sum

    def count_strategies_holding(self, symbol):
        return self.strategies

    def update_position_qty(self, symbol, new_qty):
        self.qty_updates.append((symbol, new_qty))

    def close_positions_not_in_broker(self, broker_symbols):
        return []

    def position_opened(self, pos, asset_class):
        pass


def test_reconcile_drift_no_op_when_strategies_sum_matches_broker():
    book = PositionBook()
    book.add(_trader_pos("AAPL", qty=10))
    alp = _fake_alpaca(positions=[_broker_position("AAPL", qty="30")])
    # Two other strategies hold 20 between them; book has 10; broker shows 30.
    fake_mysql = _FakeMySQLStore(local_sum=30.0, strategies=3)
    r = Reconciler(alp, ac_configs={}, mysql_store=fake_mysql)
    report = r.reconcile(book)
    assert report.drift == []
    assert report.drift_corrected == []
    assert report.drift_ambiguous == []
    assert book.get("AAPL").qty == 10
    assert fake_mysql.qty_updates == []


def test_reconcile_drift_corrected_when_sole_owner():
    book = PositionBook()
    book.add(_trader_pos("AAPL", qty=10))
    alp = _fake_alpaca(positions=[_broker_position("AAPL", qty="7")])
    fake_mysql = _FakeMySQLStore(local_sum=10.0, strategies=1)
    r = Reconciler(alp, ac_configs={}, mysql_store=fake_mysql)
    report = r.reconcile(book)
    assert report.drift == [("AAPL", 10, 7.0)]  # pre-correction snapshot
    assert report.drift_corrected == [("AAPL", 10, 7.0)]
    assert report.drift_ambiguous == []
    assert fake_mysql.qty_updates == [("AAPL", 7.0)]
    assert book.get("AAPL").qty == 7.0


def test_reconcile_drift_ambiguous_when_multi_strategy():
    book = PositionBook()
    book.add(_trader_pos("AAPL", qty=10))
    alp = _fake_alpaca(positions=[_broker_position("AAPL", qty="7")])
    # Two strategies hold this symbol; their MySQL sum (12) doesn't match
    # broker (7) — auto-correction would corrupt the other strategy's row.
    fake_mysql = _FakeMySQLStore(local_sum=12.0, strategies=2)
    r = Reconciler(alp, ac_configs={}, mysql_store=fake_mysql)
    report = r.reconcile(book)
    assert report.drift_corrected == []
    assert report.drift_ambiguous and report.drift_ambiguous[0][0] == "AAPL"
    assert fake_mysql.qty_updates == []
    assert book.get("AAPL").qty == 10  # untouched


def test_reconcile_drift_no_mysql_falls_back_to_local_sum():
    """Without MySQL the reconciler treats the local book as the only owner."""
    book = PositionBook()
    book.add(_trader_pos("AAPL", qty=100))
    alp = _fake_alpaca(positions=[_broker_position("AAPL", qty="50")])
    r = Reconciler(alp, ac_configs={})  # mysql_store=None
    report = r.reconcile(book)
    # local_sum (100) != broker (50), strategies_holding defaults to 1
    # but mysql is None so no correction happens — logged as plain drift.
    assert report.drift == [("AAPL", 100.0, 50.0)]
    assert report.drift_corrected == []
    assert report.drift_ambiguous and report.drift_ambiguous[0] == ("AAPL", 100.0, 50.0)
    assert book.get("AAPL").qty == 100


def test_reconcile_unknown_asset_class_skips_adoption(caplog):
    book = PositionBook()
    alp = _fake_alpaca(
        positions=[_broker_position("EURUSD", asset_class="forex")],
    )
    r = Reconciler(alp, ac_configs={})
    with caplog.at_level(logging.WARNING):
        report = r.reconcile(book)
    assert report.adopted_equity == []
    assert report.adopted_crypto == []
    assert book.get("EURUSD") is None
    assert any("RECONCILE_UNKNOWN_ASSET_CLASS" in rec.message
               for rec in caplog.records)


def test_reconcile_short_position_qty_uses_abs():
    book = PositionBook()
    alp = _fake_alpaca(
        positions=[_broker_position("AAPL", qty="-10", side="short")],
    )
    r = Reconciler(alp, ac_configs={})
    r.reconcile(book)
    pos = book.get("AAPL")
    assert pos.side == "short"
    assert pos.qty == 10.0


def test_reconcile_naked_crypto_logs_every_cycle(caplog):
    book = PositionBook()
    alp = _fake_alpaca(
        positions=[_broker_position("BTCUSD", asset_class="crypto",
                                    qty="0.5", entry="50000.0")],
    )
    r = Reconciler(alp, ac_configs={})
    with caplog.at_level(logging.WARNING):
        r.reconcile(book)
        caplog.clear()
        r.reconcile(book)
    assert any("ADOPTED_CRYPTO_NAKED" in rec.message
               for rec in caplog.records)


def test_reconcile_does_not_double_log_adoption_for_existing_adopted_position():
    book = PositionBook()
    alp = _fake_alpaca(
        positions=[_broker_position("AAPL", qty="10")],
    )
    r = Reconciler(alp, ac_configs={})
    r.reconcile(book)
    report2 = r.reconcile(book)
    assert report2.adopted_equity == []
    assert book.count() == 1


def test_reconcile_skips_adoption_when_not_in_configured_symbols():
    book = PositionBook()
    alp = _fake_alpaca(
        positions=[_broker_position("AAPL", qty="10"), _broker_position("BTCUSD", asset_class="crypto", qty="0.5")],
    )
    # Only AAPL is configured, BTCUSD is not
    r = Reconciler(alp, ac_configs={}, configured_symbols=["AAPL"])
    report = r.reconcile(book)
    assert report.adopted_equity == ["AAPL"]
    assert report.adopted_crypto == []
    assert book.count() == 1
    assert book.get("AAPL") is not None
    assert book.get("BTCUSD") is None


def test_reconcile_skips_adoption_when_owned_by_another_strategy():
    book = PositionBook()
    alp = _fake_alpaca(
        positions=[_broker_position("AAPL", qty="10")],
    )
    # Fake MySQL indicates another strategy holds AAPL (strategies = 1)
    fake_mysql = _FakeMySQLStore(strategies=1)
    r = Reconciler(alp, ac_configs={}, mysql_store=fake_mysql)
    report = r.reconcile(book)
    assert report.adopted_equity == []
    assert book.count() == 0


def test_adopted_equity_position_has_role_adopted_coid():
    """Adoption must stamp a parseable role=adopted COID on the OpenPosition."""
    from unittest.mock import MagicMock
    from state.reconciler import Reconciler
    from state.position_book import PositionBook
    from broker.client_order_id import parse_client_order_id

    alpaca = MagicMock()
    alpaca.get_positions.return_value = [{
        "symbol": "AAPL",
        "qty": "10",
        "side": "long",
        "avg_entry_price": "100.00",
        "asset_class": "us_equity",
    }]
    alpaca.list_orders.return_value = []  # no bracket data

    mysql = MagicMock()
    mysql.strategy_name = "vwap_wave"
    mysql.close_positions_not_in_broker.return_value = []
    mysql.count_strategies_holding.return_value = 0

    book = PositionBook()
    rec = Reconciler(alpaca, mysql_store=mysql, configured_symbols=["AAPL"])
    rec.reconcile(book)

    pos = book.get("AAPL")
    assert pos is not None
    parsed = parse_client_order_id(pos.client_order_id)
    assert parsed is not None, f"adopted position COID is not parseable: {pos.client_order_id!r}"
    assert parsed["strategy"] == "vwap_wave"
    assert parsed["setup"] == "adopted"
    assert parsed["symbol"] == "AAPL"
    assert parsed["role"] == "adopted"


def test_adopted_crypto_position_has_role_adopted_coid():
    from unittest.mock import MagicMock
    from state.reconciler import Reconciler
    from state.position_book import PositionBook
    from broker.client_order_id import parse_client_order_id

    alpaca = MagicMock()
    alpaca.get_positions.return_value = [{
        "symbol": "BTCUSD",
        "qty": "0.5",
        "side": "long",
        "avg_entry_price": "50000.00",
        "current_price": "50100.00",
        "asset_class": "crypto",
    }]
    alpaca.get_crypto_bars.return_value = []  # no bars; ATR computation skipped

    mysql = MagicMock()
    mysql.strategy_name = "vwap_wave"
    mysql.close_positions_not_in_broker.return_value = []
    mysql.count_strategies_holding.return_value = 0

    book = PositionBook()
    rec = Reconciler(alpaca, mysql_store=mysql, configured_symbols=["BTCUSD"])
    rec.reconcile(book)

    pos = book.get("BTCUSD")
    assert pos is not None
    parsed = parse_client_order_id(pos.client_order_id)
    assert parsed is not None
    assert parsed["role"] == "adopted"
    assert parsed["symbol"] == "BTCUSD"
