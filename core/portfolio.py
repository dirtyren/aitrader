from dataclasses import dataclass, field
from typing import Optional
import pandas as pd


@dataclass
class PortfolioAsset:
    ticker: str
    name: str
    asset_class: str          # "equity", "commodity", "bond", etc.
    target_weight: float      # 0.0 - 1.0
    min_weight: float = 0.0
    max_weight: float = 1.0

    def clamp(self, weight: float) -> float:
        """Clamp a proposed weight to [min_weight, max_weight]."""
        return max(self.min_weight, min(self.max_weight, weight))


class Portfolio:
    """Defines the universe of assets and their target weights."""

    def __init__(self, assets: list[PortfolioAsset],
                 rebalance_threshold: float = 0.05,
                 max_single_asset: float = 0.40):
        self.assets = assets
        self.rebalance_threshold = rebalance_threshold
        self.max_single_asset = max_single_asset
        # validate weights sum to ~1.0
        total = sum(a.target_weight for a in assets)
        if not (0.99 <= total <= 1.01):
            raise ValueError(f"Portfolio target weights must sum to 1.0, got {total:.4f}")

    @classmethod
    def from_config(cls, config: dict) -> "Portfolio":
        """Build Portfolio from settings.yaml config dict."""
        pcfg = config["portfolio"]
        assets = [
            PortfolioAsset(
                ticker=a["ticker"],
                name=a["name"],
                asset_class=a["asset_class"],
                target_weight=a["target_weight"],
                min_weight=a.get("min_weight", 0.0),
                max_weight=a.get("max_weight", 1.0),
            )
            for a in pcfg["assets"]
        ]
        return cls(
            assets=assets,
            rebalance_threshold=pcfg.get("rebalance_threshold", 0.05),
            max_single_asset=pcfg.get("max_single_asset", 0.40),
        )

    @property
    def tickers(self) -> list[str]:
        return [a.ticker for a in self.assets]

    def get_asset(self, ticker: str) -> Optional[PortfolioAsset]:
        return next((a for a in self.assets if a.ticker == ticker), None)

    def current_weights(self, positions: dict[str, float],
                        total_equity: float) -> dict[str, float]:
        """
        Compute current weight of each asset.
        positions: {ticker: dollar_value}
        Returns {ticker: weight} for all portfolio assets (0.0 if not held).
        """
        if total_equity <= 0:
            return {a.ticker: 0.0 for a in self.assets}
        return {
            a.ticker: positions.get(a.ticker, 0.0) / total_equity
            for a in self.assets
        }

    def drift(self, positions: dict[str, float],
              total_equity: float) -> dict[str, float]:
        """
        Compute signed drift = current_weight - target_weight per asset.
        Positive = overweight, negative = underweight.
        """
        current = self.current_weights(positions, total_equity)
        return {
            a.ticker: current[a.ticker] - a.target_weight
            for a in self.assets
        }

    def needs_rebalance(self, positions: dict[str, float],
                        total_equity: float) -> bool:
        """True if any asset's |drift| exceeds rebalance_threshold."""
        return any(
            abs(d) > self.rebalance_threshold
            for d in self.drift(positions, total_equity).values()
        )

    def regime_adjusted_targets(self, signal_map: dict[str, object]) -> dict[str, float]:
        """
        Compute regime-adjusted target weights.

        signal_map: {ticker: SignalData}
        Each asset's target weight is scaled by its SignalData.allocation_pct,
        then the result is normalized so all weights sum to 1.0.
        Respects each asset's min_weight/max_weight constraints.

        If a ticker has no signal, use its base target_weight * 0.5 (cautious default).
        """
        raw: dict[str, float] = {}
        for asset in self.assets:
            sig = signal_map.get(asset.ticker)
            if sig is not None:
                raw[asset.ticker] = asset.clamp(asset.target_weight * sig.allocation_pct)
            else:
                raw[asset.ticker] = asset.clamp(asset.target_weight * 0.5)

        # Normalize to sum = 1.0
        total = sum(raw.values())
        if total <= 0:
            # All signals flat — equal weight
            n = len(self.assets)
            return {a.ticker: 1.0 / n for a in self.assets}
        return {ticker: w / total for ticker, w in raw.items()}

    def summary(self, positions: dict[str, float],
                total_equity: float) -> pd.DataFrame:
        """Return a DataFrame with ticker, name, target_weight, current_weight, drift."""
        current = self.current_weights(positions, total_equity)
        drifts = self.drift(positions, total_equity)
        rows = [
            {
                "ticker": a.ticker,
                "name": a.name,
                "asset_class": a.asset_class,
                "target_weight": a.target_weight,
                "current_weight": current[a.ticker],
                "drift": drifts[a.ticker],
            }
            for a in self.assets
        ]
        return pd.DataFrame(rows).set_index("ticker")
