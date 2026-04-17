"""
feature_eng.py — Feature engineering for the regime_trader HMM pipeline.

Transforms raw OHLCV price/volume series into the three-feature DataFrame
expected by HMMModel.fit() and HMMModel.predict_current_regime().
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def build_features(
    price_series: pd.Series,
    volume_series: pd.Series = None,
    window: int = 20,
) -> pd.DataFrame:
    """Build HMM input features from a price (and optionally volume) series.

    Computes three features:

    * ``log_return``    — daily log return: ln(P_t / P_{t-1})
    * ``volatility``    — rolling ``window``-day standard deviation of log returns
    * ``volume_change`` — daily pct change in volume (0.0 when volume unavailable)

    The first ``window`` bars are dropped because the rolling volatility window
    cannot be fully populated before that point.

    Parameters
    ----------
    price_series : pd.Series
        Adjusted closing prices indexed by date.
    volume_series : pd.Series, optional
        Daily volume indexed by the same dates as *price_series*.
        When omitted, ``volume_change`` is set to 0.0 for all bars.
    window : int
        Rolling window length for volatility calculation (default 20).

    Returns
    -------
    pd.DataFrame
        Columns: ``log_return``, ``volatility``, ``volume_change``.
        Index matches *price_series* with the leading NaN rows dropped.
    """
    log_return = np.log(price_series / price_series.shift(1))
    volatility = log_return.rolling(window=window, min_periods=window).std()

    if volume_series is not None:
        volume_change = volume_series.pct_change().fillna(0)
    else:
        volume_change = pd.Series(0.0, index=price_series.index)

    df = pd.DataFrame(
        {
            "log_return": log_return,
            "volatility": volatility,
            "volume_change": volume_change,
        },
        index=price_series.index,
    )

    return df.dropna()
