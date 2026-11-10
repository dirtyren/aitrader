from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Optional
import pandas as pd


@dataclass(frozen=True)
class Quote:
    venue: str
    symbol: str
    ts_utc_ms: Optional[int]  # exchange timestamp; None when the exchange gave none
    bid: float
    ask: float
    recv_ts_utc_ms: Optional[int] = None  # local receive time
    bid_sz: Optional[float] = None
    ask_sz: Optional[float] = None


class VenueConnector(ABC):
    """Abstract base class for all venue connectors.

    Every connector must implement load_quotes returning a pandas DataFrame
    with columns ['ts_utc_ms', 'bid', 'ask'] sorted ascending by ts_utc_ms.
    """

    @abstractmethod
    def load_quotes(
        self, symbol: str, start: datetime, end: datetime
    ) -> pd.DataFrame:
        """Load quotes for a symbol between start and end.

        Args:
            symbol: Normalized symbol string (e.g. 'BTC/USDT').
            start: UTC start datetime (inclusive).
            end: UTC end datetime (inclusive).

        Returns:
            pd.DataFrame with columns ['ts_utc_ms', 'bid', 'ask'] sorted ascending by ts_utc_ms.
        """
        pass
