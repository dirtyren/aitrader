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
