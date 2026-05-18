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
