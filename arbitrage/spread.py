import pandas as pd


def gross_spread(aligned: pd.DataFrame, venue_buy: str, venue_sell: str) -> pd.Series:
    """Gross spread in quote units: buy on venue_buy (pay ask), sell on venue_sell (receive bid).

    Positive spread indicates a profitable opportunity before fees and costs.
    """
    return aligned["bid_" + venue_sell] - aligned["ask_" + venue_buy]


def mid_quote(aligned: pd.DataFrame, venue: str) -> pd.Series:
    """Mid quote for a given venue: (bid + ask) / 2."""
    return (aligned["bid_" + venue] + aligned["ask_" + venue]) / 2
