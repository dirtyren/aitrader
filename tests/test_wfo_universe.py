from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from backtest.wfo.universe import scan_alpaca_universe


def _asset(symbol, asset_class="us_equity", *, status="active", tradable=True):
    return {"symbol": symbol, "class": asset_class, "status": status, "tradable": tradable}


def _bars_with_volume(symbol, *, dollar_volume):
    base = datetime(2026, 5, 1, tzinfo=timezone.utc)
    px = 100.0
    qty = dollar_volume / px
    return [
        {"t": (base + timedelta(days=i)).isoformat().replace("+00:00", "Z"),
         "o": px, "h": px, "l": px, "c": px, "v": qty}
        for i in range(20)
    ]


def _client(asset_list, dollar_volumes):
    """dollar_volumes: dict[symbol] -> float."""
    client = MagicMock()
    client.get_assets.return_value = asset_list
    client.get_stock_bars.side_effect = lambda symbol, tf, start, end: \
        _bars_with_volume(symbol, dollar_volume=dollar_volumes[symbol])
    client.get_crypto_bars.side_effect = lambda symbol, tf, start, end: \
        _bars_with_volume(symbol, dollar_volume=dollar_volumes[symbol])
    return client


def test_scan_filters_inactive_and_untradable(tmp_path):
    client = _client(
        [_asset("AAPL"), _asset("FOO", status="inactive"),
         _asset("BAR", tradable=False)],
        {"AAPL": 50_000_000},
    )
    out = scan_alpaca_universe(client, classes=["us_equity"],
                               min_dollar_volume_20d=1_000_000,
                               top_n_per_class={"us_equity": 10},
                               cache_dir=tmp_path,
                               asof_date=date(2026, 5, 19))
    assert out == [("AAPL", "us_equity")]


def test_scan_drops_below_volume_floor(tmp_path):
    client = _client(
        [_asset("AAPL"), _asset("MEH"), _asset("PENNY")],
        {"AAPL": 50_000_000, "MEH": 8_000_000, "PENNY": 100_000},
    )
    out = scan_alpaca_universe(client, classes=["us_equity"],
                               min_dollar_volume_20d=5_000_000,
                               top_n_per_class={"us_equity": 10},
                               cache_dir=tmp_path,
                               asof_date=date(2026, 5, 19))
    assert sorted(out) == [("AAPL", "us_equity"), ("MEH", "us_equity")]


def test_scan_top_n_caps_per_class(tmp_path):
    client = _client(
        [_asset("AAA"), _asset("BBB"), _asset("CCC"), _asset("DDD")],
        {"AAA": 90_000_000, "BBB": 80_000_000,
         "CCC": 70_000_000, "DDD": 60_000_000},
    )
    out = scan_alpaca_universe(client, classes=["us_equity"],
                               min_dollar_volume_20d=1_000_000,
                               top_n_per_class={"us_equity": 2},
                               cache_dir=tmp_path,
                               asof_date=date(2026, 5, 19))
    # Sort-by-liquidity-desc → AAA, BBB win
    assert out == [("AAA", "us_equity"), ("BBB", "us_equity")]


def test_scan_top_n_none_means_no_cap(tmp_path):
    client = _client(
        [_asset("X"), _asset("Y"), _asset("Z")],
        {"X": 90_000_000, "Y": 80_000_000, "Z": 70_000_000},
    )
    out = scan_alpaca_universe(client, classes=["us_equity"],
                               min_dollar_volume_20d=1_000_000,
                               top_n_per_class={"us_equity": None},
                               cache_dir=tmp_path,
                               asof_date=date(2026, 5, 19))
    assert len(out) == 3


def test_scan_uses_cache_on_repeat(tmp_path):
    client = _client([_asset("AAPL")], {"AAPL": 50_000_000})
    kwargs = dict(classes=["us_equity"], min_dollar_volume_20d=1_000_000,
                  top_n_per_class={"us_equity": 10}, cache_dir=tmp_path,
                  asof_date=date(2026, 5, 19))
    scan_alpaca_universe(client, **kwargs)
    first_calls = client.get_assets.call_count + client.get_stock_bars.call_count
    scan_alpaca_universe(client, **kwargs)
    second_calls = client.get_assets.call_count + client.get_stock_bars.call_count
    # Second call should not have hit the broker
    assert second_calls == first_calls
