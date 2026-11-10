import hashlib
import io
import zipfile
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from arbitrage.connectors import binance_archive as ba
from arbitrage.connectors.binance_archive import (
    ChecksumMismatchError,
    download_and_cache,
    parse_bookticker_csv,
    verify_checksum,
)

FIXTURE = Path(__file__).parent / "fixtures" / "bookTicker_sample.csv"

REAL_CSV = (
    "update_id,best_bid_price,best_bid_qty,best_ask_price,best_ask_qty,"
    "transaction_time,event_time\n"
    "3,42313.9,10.7,42314.0,2.9,1704067200005,1704067200013\n"
    "1,42313.9,10.7,42314.0,2.6,1704067200006,1704067200011\n"
)


def test_parse_bookticker_csv():
    df = parse_bookticker_csv(FIXTURE)
    assert list(df.columns) == ["ts_utc_ms", "bid", "ask"]
    assert len(df) == 3
    assert df["ts_utc_ms"].is_monotonic_increasing


def test_parse_real_binance_header(tmp_path):
    p = tmp_path / "real.csv"
    p.write_text(REAL_CSV)
    df = parse_bookticker_csv(p)
    assert list(df.columns) == ["ts_utc_ms", "bid", "ask"]
    # uses event_time, sorted ascending
    assert df["ts_utc_ms"].tolist() == [1704067200011, 1704067200013]
    assert df["bid"].iloc[0] == 42313.9


def test_parse_unknown_header_raises(tmp_path):
    p = tmp_path / "bad.csv"
    p.write_text("a,b,c\n1,2,3\n")
    with pytest.raises(ValueError):
        parse_bookticker_csv(p)


def test_checksum_mismatch_raises(tmp_path):
    bad = tmp_path / "x.csv"
    bad.write_text("corrupt")
    cs = tmp_path / "x.csv.CHECKSUM"
    cs.write_text("0" * 64 + "  x.csv\n")
    with pytest.raises(ChecksumMismatchError):
        verify_checksum(bad, cs)


def test_checksum_missing_file_raises(tmp_path):
    bad = tmp_path / "x.csv"
    bad.write_text("corrupt")
    with pytest.raises(ChecksumMismatchError):
        verify_checksum(bad, tmp_path / "x.csv.CHECKSUM")


def test_checksum_ok(tmp_path):
    f = tmp_path / "x.csv"
    f.write_text("hello")
    digest = hashlib.sha256(b"hello").hexdigest()
    cs = tmp_path / "x.csv.CHECKSUM"
    cs.write_text(f"{digest}  x.csv\n")
    verify_checksum(f, cs)  # no raise


def _zip_bytes() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("BTCUSDT-bookTicker-2024-01-01.csv", REAL_CSV)
    return buf.getvalue()


def _fake_fetch(zip_data: bytes, checksum: str):
    calls = []

    def fetch(url: str, dest: Path) -> None:
        calls.append(url)
        if url.endswith(".CHECKSUM"):
            dest.write_text(checksum)
        else:
            dest.write_bytes(zip_data)

    return fetch, calls


def test_download_and_cache_roundtrip(tmp_path, monkeypatch):
    data = _zip_bytes()
    fetch, calls = _fake_fetch(data, hashlib.sha256(data).hexdigest() + "  x.zip\n")
    monkeypatch.setattr(ba, "_fetch", fetch)
    out = download_and_cache("BTCUSDT", "um", date(2024, 1, 1), tmp_path)
    assert out.suffix == ".parquet" and out.parent == tmp_path
    assert calls[0] == (
        "https://data.binance.vision/data/futures/um/daily/bookTicker/BTCUSDT/"
        "BTCUSDT-bookTicker-2024-01-01.zip"
    )
    assert calls[1] == calls[0] + ".CHECKSUM"
    df = pd.read_parquet(out)
    assert list(df.columns) == ["ts_utc_ms", "bid", "ask"]
    assert len(df) == 2
    # cached: second call does not fetch again
    download_and_cache("BTCUSDT", "um", date(2024, 1, 1), tmp_path)
    assert len(calls) == 2


def test_download_bad_checksum_raises_and_caches_nothing(tmp_path, monkeypatch):
    fetch, _ = _fake_fetch(_zip_bytes(), "0" * 64 + "  x.zip\n")
    monkeypatch.setattr(ba, "_fetch", fetch)
    with pytest.raises(ChecksumMismatchError):
        download_and_cache("BTCUSDT", "um", date(2024, 1, 1), tmp_path)
    assert list(tmp_path.glob("*.parquet")) == []


def test_spot_market_unsupported(tmp_path):
    with pytest.raises(ValueError, match="spot"):
        download_and_cache("BTCUSDT", "spot", date(2024, 1, 1), tmp_path)


def test_connector_load_quotes_filters_window(tmp_path, monkeypatch):
    from datetime import datetime, timezone

    data = _zip_bytes()
    fetch, _ = _fake_fetch(data, hashlib.sha256(data).hexdigest() + "  x.zip\n")
    monkeypatch.setattr(ba, "_fetch", fetch)
    conn = ba.BinanceArchiveConnector(market="um", cache_dir=tmp_path)
    s = datetime(2024, 1, 1, tzinfo=timezone.utc)
    df = conn.load_quotes("BTC/USDT", s, s.replace(hour=23))
    assert list(df.columns) == ["ts_utc_ms", "bid", "ask"] and len(df) == 2
    # window excluding both rows
    df2 = conn.load_quotes("BTC/USDT", s.replace(hour=12), s.replace(hour=23))
    assert df2.empty


def _http_error(status: int):
    import requests

    resp = requests.Response()
    resp.status_code = status
    return requests.HTTPError(f"{status}", response=resp)


def _fetch_only_jan1(data: bytes, status: int = 404):
    good, _ = _fake_fetch(data, hashlib.sha256(data).hexdigest() + "  x.zip\n")

    def fetch(url: str, dest: Path) -> None:
        if "2024-01-01" not in url:
            raise _http_error(status)
        good(url, dest)

    return fetch


def test_load_quotes_skips_days_without_archive(tmp_path, monkeypatch, caplog):
    from datetime import datetime, timezone

    monkeypatch.setattr(ba, "_fetch", _fetch_only_jan1(_zip_bytes()))
    conn = ba.BinanceArchiveConnector(market="um", cache_dir=tmp_path)
    s = datetime(2024, 1, 1, tzinfo=timezone.utc)
    # range spans day before (missing), Jan 1 (present), Jan 2 (missing)
    with caplog.at_level("WARNING"):
        df = conn.load_quotes("BTCUSDT", s - pd.Timedelta(days=1), s.replace(day=2, hour=23))
    assert len(df) == 2
    assert "no archive" in caplog.text
    # entirely unavailable range -> empty frame, correct columns, no raise
    empty = conn.load_quotes("BTCUSDT", s.replace(day=5), s.replace(day=6))
    assert empty.empty and list(empty.columns) == ["ts_utc_ms", "bid", "ask"]


def test_load_quotes_non_404_error_propagates(tmp_path, monkeypatch):
    import requests
    from datetime import datetime, timezone

    monkeypatch.setattr(ba, "_fetch", _fetch_only_jan1(_zip_bytes(), status=500))
    conn = ba.BinanceArchiveConnector(market="um", cache_dir=tmp_path)
    s = datetime(2024, 1, 1, tzinfo=timezone.utc)
    with pytest.raises(requests.HTTPError):
        conn.load_quotes("BTCUSDT", s, s.replace(day=2))


def test_load_quotes_filters_each_day_before_concat(tmp_path, monkeypatch):
    from datetime import datetime, timezone

    data = _zip_bytes()
    fetch, _ = _fake_fetch(data, hashlib.sha256(data).hexdigest() + "  x.zip\n")
    monkeypatch.setattr(ba, "_fetch", fetch)
    conn = ba.BinanceArchiveConnector(market="um", cache_dir=tmp_path)
    s = datetime(2024, 1, 1, tzinfo=timezone.utc)
    # window starts after the first row (…011) but includes the second (…013)
    start = datetime.fromtimestamp(1704067200012 / 1000, tz=timezone.utc)
    seen = []
    real_concat = pd.concat

    def spy(objs, *a, **k):
        objs = list(objs)
        seen.extend(len(o) for o in objs)
        return real_concat(objs, *a, **k)

    monkeypatch.setattr(ba.pd, "concat", spy)
    df = conn.load_quotes("BTCUSDT", start, s.replace(hour=23))
    assert df["ts_utc_ms"].tolist() == [1704067200013]
    assert seen == [1]  # only the filtered slice was concatenated, not the full day (2 rows)
