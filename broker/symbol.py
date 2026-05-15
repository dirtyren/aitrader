from __future__ import annotations

_CRYPTO_QUOTES = {"USD", "USDT", "USDC", "EUR", "GBP", "BTC", "ETH"}


def asset_class_of(symbol: str) -> str:
    s = symbol.upper()
    if "/" in s:
        return "crypto"
    for q in _CRYPTO_QUOTES:
        if s.endswith(q) and len(s) > len(q) and s[: -len(q)] not in {"USD"}:
            base = s[: -len(q)]
            if base.isalpha() and 2 <= len(base) <= 5:
                return "crypto"
    return "equity"


def normalize_for_api(symbol: str, asset_class: str) -> str:
    if asset_class == "equity":
        return symbol.upper()
    if asset_class == "crypto":
        s = symbol.upper()
        if "/" in s:
            return s
        for q in _CRYPTO_QUOTES:
            if s.endswith(q) and len(s) > len(q):
                return f"{s[: -len(q)]}/{q}"
        return s
    raise ValueError(f"Unknown asset_class: {asset_class}")
