from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class SignalData:
    regime: str
    confidence: float
    allocation_pct: float      # 0.0 to 1.0 (e.g., 0.95 = 95%)
    leverage: float            # e.g., 1.0 = no leverage, 1.25 = 25% leverage
    high_uncertainty: bool
    stable: bool
    notes: str = ""


class BaseStrategy(ABC):
    @abstractmethod
    def compute_signal(self, regime_result: dict) -> SignalData:
        """Translate a regime classification dict to a SignalData."""
        ...

    @abstractmethod
    def name(self) -> str:
        ...
