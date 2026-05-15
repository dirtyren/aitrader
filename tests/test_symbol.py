from broker.symbol import normalize_for_api, asset_class_of


def test_equity_passthrough():
    assert normalize_for_api("AAPL", "equity") == "AAPL"
    assert asset_class_of("AAPL") == "equity"


def test_crypto_normalization():
    assert normalize_for_api("BTC/USD", "crypto") == "BTC/USD"
    assert asset_class_of("BTC/USD") == "crypto"
    assert asset_class_of("btc/usd") == "crypto"


def test_legacy_crypto_form_normalized():
    assert normalize_for_api("BTCUSD", "crypto") == "BTC/USD"
