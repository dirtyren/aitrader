"""
data_loader.py — Market data acquisition layer for the regime_trader system.

Fetches OHLCV data from Yahoo Finance via yfinance.
"""

from __future__ import annotations

import logging
from typing import Optional

import pandas as pd
import yfinance as yf

logger = logging.getLogger(__name__)


class DataLoader:
    """Fetches and caches historical OHLCV data from Yahoo Finance.

    Parameters
    ----------
    config : dict
        Top-level config dict (from settings.yaml).  Reads:
        - config["tickers"]["primary"]  — default ticker list
        - config["hmm"]["train_days"]   — default look-back window
    """

    def __init__(self, config: dict) -> None:
        self.tickers: list[str] = config["tickers"]["primary"]
        self.train_days: int = config["hmm"]["train_days"]

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def fetch_historical(self, ticker: str, days: int = None) -> pd.DataFrame:
        """Fetch daily OHLCV data for *ticker* using yfinance.

        Parameters
        ----------
        ticker : str
            Equity symbol (e.g. ``"SPY"``).
        days : int, optional
            Number of calendar days to look back.  Defaults to
            ``self.train_days``.

        Returns
        -------
        pd.DataFrame
            Columns: Open, High, Low, Close, Volume.
            Index: DatetimeIndex (daily frequency).

        Raises
        ------
        ValueError
            If yfinance returns an empty DataFrame for the given ticker.
        """
        if days is None:
            days = self.train_days

        logger.debug("Fetching %d days of daily data for %s", days, ticker)

        df = yf.download(
            ticker,
            period=f"{days}d",
            interval="1d",
            auto_adjust=True,
            progress=False,
        )

        if df.empty:
            raise ValueError(
                f"yfinance returned no data for ticker '{ticker}' "
                f"(period={days}d, interval=1d)"
            )

        # yfinance may return a MultiIndex when downloading a single ticker
        # with certain versions — flatten if necessary.
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        # Ensure standard column names
        df = df[["Open", "High", "Low", "Close", "Volume"]].copy()
        df.index = pd.to_datetime(df.index)

        logger.debug(
            "Fetched %d rows for %s (first=%s, last=%s)",
            len(df),
            ticker,
            df.index[0].date(),
            df.index[-1].date(),
        )
        return df

    def fetch_multiple(
        self,
        tickers: list[str] = None,
        days: int = None,
    ) -> dict[str, pd.DataFrame]:
        """Fetch historical OHLCV data for each ticker in *tickers*.

        Parameters
        ----------
        tickers : list[str], optional
            Symbols to fetch.  Defaults to ``self.tickers``.
        days : int, optional
            Number of calendar days to look back.  Defaults to
            ``self.train_days``.

        Returns
        -------
        dict[str, pd.DataFrame]
            Mapping ``{ticker: df}`` for every symbol that was
            successfully downloaded.  Failed tickers are logged and
            omitted from the result.
        """
        if tickers is None:
            tickers = self.tickers
        if days is None:
            days = self.train_days

        result: dict[str, pd.DataFrame] = {}
        for ticker in tickers:
            try:
                result[ticker] = self.fetch_historical(ticker, days=days)
            except Exception as exc:
                logger.warning(
                    "fetch_multiple: skipping %s — %s", ticker, exc
                )
        return result

    def get_latest_price(self, ticker: str) -> float:
        """Return the most recent closing price for *ticker*.

        Uses yfinance to download the last 5 days (to handle weekends /
        holidays) and returns the most recent close.

        Parameters
        ----------
        ticker : str
            Equity symbol.

        Returns
        -------
        float
            Latest adjusted close price.

        Raises
        ------
        ValueError
            If no price data is available.
        """
        df = yf.download(
            ticker,
            period="5d",
            interval="1d",
            auto_adjust=True,
            progress=False,
        )

        if df.empty:
            raise ValueError(
                f"yfinance returned no data for ticker '{ticker}' when fetching latest price."
            )

        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        latest_close = float(df["Close"].iloc[-1])
        logger.debug("Latest price for %s: %.4f", ticker, latest_close)
        return latest_close
