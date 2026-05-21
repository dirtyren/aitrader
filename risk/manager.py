from __future__ import annotations
import logging
from dataclasses import dataclass

from risk.circuit_breakers import CircuitBreaker
from risk.filters import FilterPipeline
from risk.sizing import SizingConfig, size_position
from state.daily_ledger import DailyLedger
from state.position_book import PositionBook
from strategies.base_setup import SetupSignal

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RiskDecision:
    approved: bool
    qty: float
    notional: float
    reason: str = ""

    @classmethod
    def reject(cls, reason: str) -> "RiskDecision":
        return cls(approved=False, qty=0.0, notional=0.0, reason=reason)


class RiskManager:
    def __init__(self,
                 circuit_breaker: CircuitBreaker,
                 pipeline: FilterPipeline,
                 sizing_equity: SizingConfig,
                 sizing_crypto: SizingConfig,
                 ledger: DailyLedger,
                 book: PositionBook):
        self.circuit_breaker = circuit_breaker
        self.pipeline = pipeline
        self.sizing_equity = sizing_equity
        self.sizing_crypto = sizing_crypto
        self.ledger = ledger
        self.book = book
        self.available_cash: float | None = None

    def update_equity(self, equity: float) -> None:
        self.ledger.equity = equity
        self.circuit_breaker.peak_equity = max(self.circuit_breaker.peak_equity, equity)

    def update_cash(self, cash: float | None) -> None:
        self.available_cash = float(cash) if cash is not None else None

    def evaluate(self, signal: SetupSignal, ctx, asset_class: str) -> RiskDecision:
        result = self.pipeline.check(signal, ctx, self.ledger, self.book)
        if not result.passed:
            logger.info("FILTER_REJECT symbol=%s setup=%s reason=%s",
                        signal.symbol, signal.setup, result.reason)
            return RiskDecision.reject(result.reason)

        sizing = self.sizing_crypto if asset_class == "crypto" else self.sizing_equity
        try:
            qty, notional = size_position(
                self.ledger.equity, signal.entry, signal.stop, sizing,
                available_cash=self.available_cash,
            )
        except ValueError as exc:
            return RiskDecision.reject(str(exc))

        # Apply circuit-breaker L1 (halve sizes).
        # CircuitBreaker doesn't store .level as an attribute; getattr-default keeps this
        # safe when level isn't set yet (i.e. before any .check() call).
        if getattr(self.circuit_breaker, "level", 0) == 1:
            qty *= 0.5
            notional *= 0.5

        if qty <= 0:
            return RiskDecision.reject("sized to zero")

        return RiskDecision(approved=True, qty=qty, notional=notional)
