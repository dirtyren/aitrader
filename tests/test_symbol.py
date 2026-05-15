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


def test_short_alpha_with_quote_suffix_is_crypto():
    # ETHUSD → ETH/USD (crypto-like base + quote)
    assert asset_class_of("ETHUSD") == "crypto"
    assert normalize_for_api("ETHUSD", "crypto") == "ETH/USD"


def test_long_or_numeric_ticker_with_usd_is_equity():
    # Real-world plain equity tickers like 'BIDU', 'BABA' should never be misclassified.
    assert asset_class_of("AAPL") == "equity"
    # Defensive: a ticker that doesn't have a recognizable crypto base shape stays equity.
    assert asset_class_of("VFIAX") == "equity"
