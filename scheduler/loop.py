from __future__ import annotations
import logging
from dataclasses import dataclass
from datetime import datetime

from broker.order_executor import OrderExecutor
from core.bar import Bar
from core.position_manager import PositionAction, PositionManager
from core.session import SessionContext
from risk.manager import RiskManager
from state.daily_ledger import DailyLedger, TradeRecord
from state.position_book import OpenPosition, PositionBook
from strategies.base_setup import BaseSetup

logger = logging.getLogger(__name__)

_EXIT_KINDS = ("stop", "target", "time_stop")


def record_exits_to_ledger(ledger: DailyLedger, symbol: str,
                           actions: list[PositionAction], last_bar: Bar,
                           pos_before: OpenPosition | None) -> list[TradeRecord]:
    """Append a TradeRecord per stop/target/time_stop action and return them.

    Uses pos_before because PositionManager.on_bar closes the book entry on
    exit kinds. R_realized is computed against initial_risk_per_share so
    breakeven-moved positions still report the original risk.
    """
    recorded: list[TradeRecord] = []
    if pos_before is None:
        return recorded
    for a in actions:
        if a.kind not in _EXIT_KINDS:
            continue
        sign = 1 if pos_before.side == "long" else -1
        pnl = sign * (a.price - pos_before.entry_px) * pos_before.qty
        risk = pos_before.initial_risk_per_share
        r_realized = (sign * (a.price - pos_before.entry_px)) / risk if risk > 0 else 0.0
        rec = TradeRecord(
            symbol=symbol, setup=pos_before.setup,
            entry_ts=pos_before.opened_at, exit_ts=last_bar.ts,
            entry_px=pos_before.entry_px, exit_px=a.price,
            side=pos_before.side, qty=pos_before.qty,
            R_realized=r_realized, pnl_usd=pnl,
        )
        ledger.record(rec)
        recorded.append(rec)
        logger.info("POSITION_CLOSED symbol=%s reason=%s exit=%.4f r=%.2f pnl=%.2f",
                    symbol, a.kind, a.price, r_realized, pnl)
    return recorded


@dataclass
class VWAPWaveEngine:
    """One-tick bar-close orchestrator.

    Phase A — manage open positions: ingest each fresh bar into the symbol's
    SessionContext, run PositionManager.on_bar, and route resulting actions
    through OrderExecutor.handle_actions. Exit actions also append a
    TradeRecord to the DailyLedger using the position snapshot taken before
    PositionManager mutated the book.

    Phase B — detect new entries: for each symbol's setups, run check(ctx),
    evaluate via RiskManager, and submit through OrderExecutor.
    """

    symbols: list[tuple[str, str]]
    contexts: dict[str, SessionContext]
    setups: dict[str, list[BaseSetup]]
    risk_manager: RiskManager
    executor: OrderExecutor
    book: PositionBook
    ledger: DailyLedger
    position_manager: PositionManager

    def tick(self, now: datetime, fresh_bars: dict[str, list[Bar]]) -> None:
        # Phase A: ingest bars + manage open positions
        for symbol, asset_class in self.symbols:
            new_bars = fresh_bars.get(symbol) or []
            for bar in new_bars:
                self.contexts[symbol].ingest(bar)
                pos_before = self.book.get(symbol)
                actions = self.position_manager.on_bar(symbol, bar)
                self._record_exits(symbol, actions, bar, pos_before)
                parent_order_id = pos_before.order_id if pos_before else None
                if actions:
                    self.executor.handle_actions(
                        actions, asset_class=asset_class,
                        parent_order_id=parent_order_id,
                    )

        # Phase B: setup detection + entries
        for symbol, asset_class in self.symbols:
            ctx = self.contexts[symbol]
            for setup in self.setups[symbol]:
                signal = setup.check(ctx)
                if signal is None:
                    continue
                decision = self.risk_manager.evaluate(signal, ctx, asset_class)
                if not decision.approved:
                    logger.info("SIGNAL_REJECTED symbol=%s setup=%s reason=%s",
                                signal.symbol, signal.setup, decision.reason)
                    continue
                self.executor.submit(signal, decision, asset_class)

    def _record_exits(self, symbol: str, actions: list[PositionAction],
                      last_bar: Bar, pos_before: OpenPosition | None) -> None:
        record_exits_to_ledger(self.ledger, symbol, actions, last_bar, pos_before)
