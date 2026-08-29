# tests/test_sector_exposure_filter.py
from datetime import datetime, timezone

from risk.filters import SectorExposureFilter
from state.position_book import OpenPosition, PositionBook
from strategies.base_setup import SetupSignal

TS = datetime(2026, 8, 28, 14, 5, tzinfo=timezone.utc)
SECTORS = {"AAA": "Tech", "BBB": "Tech", "CCC": "Tech", "XXX": "Energy"}


def _signal(symbol: str) -> SetupSignal:
    return SetupSignal(
        setup="opening_drive", symbol=symbol, side="long",
        entry=100.0, stop=98.0, target=104.0, atr=2.0, level=100.0, ts=TS,
    )


def _book(*symbols: str, setup: str = "opening_drive") -> PositionBook:
    b = PositionBook()
    for s in symbols:
        b.add(OpenPosition(
            symbol=s, setup=setup, side="long", qty=10, entry_px=100.0,
            stop_px=98.0, target_px=104.0, opened_at=TS, order_id=f"o-{s}",
        ))
    return b


def test_allows_first_position_in_sector():
    f = SectorExposureFilter(SECTORS, max_per_sector=2)
    assert f.check(_signal("AAA"), None, None, _book()).passed


def test_allows_second_position_in_sector():
    f = SectorExposureFilter(SECTORS, max_per_sector=2)
    assert f.check(_signal("BBB"), None, None, _book("AAA")).passed


def test_rejects_third_position_in_same_sector():
    f = SectorExposureFilter(SECTORS, max_per_sector=2)
    res = f.check(_signal("CCC"), None, None, _book("AAA", "BBB"))
    assert not res.passed
    assert "Tech" in res.reason


def test_other_sector_unaffected_by_a_full_sector():
    f = SectorExposureFilter(SECTORS, max_per_sector=2)
    assert f.check(_signal("XXX"), None, None, _book("AAA", "BBB")).passed


def test_none_book_passes():
    f = SectorExposureFilter(SECTORS, max_per_sector=2)
    assert f.check(_signal("AAA"), None, None, None).passed


def test_unknown_symbols_share_the_unknown_bucket():
    f = SectorExposureFilter({}, max_per_sector=1)
    res = f.check(_signal("AAA"), None, None, _book("ZZZ"))
    assert not res.passed
    assert "UNKNOWN" in res.reason


def test_other_setups_positions_do_not_consume_our_budget():
    """Scoping matters: another strategy's Tech position must not eat this
    strategy's sector budget."""
    f = SectorExposureFilter(SECTORS, max_per_sector=2,
                             setup_name="opening_drive")
    book = _book("AAA", setup="opening_drive")
    for p in _book("BBB", setup="vwap_wave").all():
        book.add(p)
    assert f.check(_signal("CCC"), None, None, book).passed


def test_unscoped_filter_counts_every_setup():
    f = SectorExposureFilter(SECTORS, max_per_sector=2, setup_name=None)
    book = _book("AAA", setup="opening_drive")
    for p in _book("BBB", setup="vwap_wave").all():
        book.add(p)
    assert not f.check(_signal("CCC"), None, None, book).passed


def test_zero_cap_rejects_everything():
    f = SectorExposureFilter(SECTORS, max_per_sector=0)
    assert not f.check(_signal("AAA"), None, None, _book()).passed


def test_filter_name_is_stable():
    assert SectorExposureFilter(SECTORS).name == "sector_exposure"
