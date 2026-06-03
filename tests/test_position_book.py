from datetime import datetime, timezone
from state.position_book import PositionBook, OpenPosition


def test_add_and_lookup():
    book = PositionBook()
    p = OpenPosition(symbol="AAPL", setup="price_discovery", side="long",
                     qty=10, entry_px=100.0, stop_px=99.0, target_px=102.0,
                     opened_at=datetime(2026, 5, 14, 14, 0, tzinfo=timezone.utc),
                     order_id="abc")
    book.add(p)
    assert book.get("AAPL") is p
    assert book.symbols() == ["AAPL"]


def test_concurrent_count():
    book = PositionBook()
    for s in ("AAPL", "MSFT", "BTC/USD"):
        book.add(OpenPosition(symbol=s, setup="x", side="long", qty=1,
                              entry_px=1.0, stop_px=0.5, target_px=2.0,
                              opened_at=datetime.now(timezone.utc), order_id="x"))
    assert book.count() == 3


def test_close_removes():
    book = PositionBook()
    p = OpenPosition(symbol="AAPL", setup="x", side="long", qty=1,
                     entry_px=1.0, stop_px=0.5, target_px=2.0,
                     opened_at=datetime.now(timezone.utc), order_id="x")
    book.add(p)
    book.close("AAPL")
    assert book.get("AAPL") is None


def test_close_marks_just_exited_until_cleared():
    book = PositionBook()
    p = OpenPosition(symbol="AAPL", setup="x", side="long", qty=1,
                     entry_px=1.0, stop_px=0.5, target_px=2.0,
                     opened_at=datetime.now(timezone.utc), order_id="x")
    book.add(p)
    assert not book.was_just_exited("AAPL")
    book.close("AAPL")
    assert book.was_just_exited("AAPL")
    book.clear_just_exited()
    assert not book.was_just_exited("AAPL")


def test_was_just_exited_only_for_actually_closed_symbols():
    book = PositionBook()
    book.close("NEVER_HELD")  # closing absent symbol shouldn't taint the set
    assert not book.was_just_exited("NEVER_HELD")


def test_aggregate_open_risk():
    book = PositionBook()
    book.add(OpenPosition(symbol="AAPL", setup="x", side="long", qty=10,
                          entry_px=100.0, stop_px=99.0, target_px=102.0,
                          opened_at=datetime.now(timezone.utc), order_id="x"))
    book.add(OpenPosition(symbol="MSFT", setup="x", side="long", qty=5,
                          entry_px=200.0, stop_px=199.0, target_px=202.0,
                          opened_at=datetime.now(timezone.utc), order_id="y"))
    # AAPL risk = 10 x 1 = 10; MSFT risk = 5 x 1 = 5; total 15
    assert book.aggregate_open_risk_usd() == 15.0


def test_open_position_adopted_defaults_false():
    p = OpenPosition(symbol="AAPL", setup="price_discovery", side="long",
                     qty=10, entry_px=100.0, stop_px=99.0, target_px=102.0,
                     opened_at=datetime(2026, 5, 14, 14, 0, tzinfo=timezone.utc),
                     order_id="abc")
    assert p.adopted is False


def test_open_position_adopted_can_be_set_true():
    p = OpenPosition(symbol="AAPL", setup="adopted", side="long",
                     qty=10, entry_px=100.0, stop_px=None, target_px=None,
                     opened_at=datetime(2026, 5, 14, 14, 0, tzinfo=timezone.utc),
                     order_id="", adopted=True)
    assert p.adopted is True


def test_open_position_with_none_stop_yields_zero_risk():
    p = OpenPosition(symbol="AAPL", setup="adopted", side="long",
                     qty=10, entry_px=100.0, stop_px=None, target_px=None,
                     opened_at=datetime(2026, 5, 14, 14, 0, tzinfo=timezone.utc),
                     order_id="", adopted=True)
    assert p.risk_per_share == 0.0
    assert p.initial_risk_per_share == 0.0
    assert p.open_risk_usd == 0.0


def test_aggregate_open_risk_skips_none_stop_positions():
    book = PositionBook()
    book.add(OpenPosition(symbol="AAPL", setup="x", side="long", qty=10,
                          entry_px=100.0, stop_px=99.0, target_px=102.0,
                          opened_at=datetime.now(timezone.utc), order_id="a"))
    book.add(OpenPosition(symbol="BTC/USD", setup="adopted", side="long", qty=1,
                          entry_px=50_000.0, stop_px=None, target_px=None,
                          opened_at=datetime.now(timezone.utc), order_id="",
                          adopted=True))
    assert book.aggregate_open_risk_usd() == 10.0  # only the AAPL position contributes


def test_symbol_normalization():
    book = PositionBook()
    p = OpenPosition(symbol="BTCUSD", setup="adopted", side="long", qty=1,
                      entry_px=50_000.0, stop_px=49000.0, target_px=51000.0,
                      opened_at=datetime.now(timezone.utc), order_id="",
                      adopted=True)
    book.add(p)
    
    # lookup with slash should work
    assert book.get("BTC/USD") is p
    assert book.get_all("BTC/USD") == [p]
    assert book.has_symbol("BTC/USD") is True
    assert book.was_just_exited("BTC/USD") is False
    
    # close with slash should work
    closed = book.close("BTC/USD")
    assert closed is p
    assert book.get("BTCUSD") is None
    assert book.get("BTC/USD") is None
    assert book.was_just_exited("BTC/USD") is True


def test_open_position_client_order_id_defaults_to_none():
    pos = OpenPosition(
        symbol="AAPL", setup="vwap_bounce", side="long", qty=1.0,
        entry_px=100.0, stop_px=99.0, target_px=101.0,
        opened_at=datetime(2026, 5, 28, tzinfo=timezone.utc), order_id="o1",
    )
    assert pos.client_order_id is None


def test_open_position_client_order_id_can_be_set():
    pos = OpenPosition(
        symbol="AAPL", setup="vwap_bounce", side="long", qty=1.0,
        entry_px=100.0, stop_px=99.0, target_px=101.0,
        opened_at=datetime(2026, 5, 28, tzinfo=timezone.utc), order_id="o1",
        client_order_id="aitrader__vwap_wave__vwap_bounce__AAPL__entry__abcd1234",
    )
    assert pos.client_order_id == "aitrader__vwap_wave__vwap_bounce__AAPL__entry__abcd1234"


def test_open_position_exit_submitted_default_false():
    pos = OpenPosition(
        symbol="COIN", setup="price_discovery", side="short",
        qty=1.0, entry_px=174.07, stop_px=175.31, target_px=171.60,
        opened_at=datetime(2026, 6, 2, 15, 0, tzinfo=timezone.utc),
        order_id="abc",
    )
    assert pos.exit_submitted is False


def test_open_position_exit_submitted_settable():
    pos = OpenPosition(
        symbol="COIN", setup="price_discovery", side="short",
        qty=1.0, entry_px=174.07, stop_px=175.31, target_px=171.60,
        opened_at=datetime(2026, 6, 2, 15, 0, tzinfo=timezone.utc),
        order_id="abc",
        exit_submitted=True,
    )
    assert pos.exit_submitted is True

