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
                           mysql_store=None,
                           positions_snapshot: dict[str, OpenPosition] | None = None,
                           ) -> list[TradeRecord]:
    """Append a TradeRecord per stop/target/time_stop action and return them.

    Uses positions_snapshot because PositionManager.on_bar may close the book
    entry on exit kinds. With multi-setup positions, each action references
    the position that triggered it via the *setup* field in PositionAction,
    so we match by setup name when multiple positions exist on the same symbol.

    If mysql_store is provided, position_closed() is called for each action.
    """
    recorded: list[TradeRecord] = []
    for a in actions:
        if a.kind not in _EXIT_KINDS:
            continue
        # Find the snapshot position for this action's setup
        pos_before = None
        if positions_snapshot and a.setup in positions_snapshot:
            pos_before = positions_snapshot[a.setup]

        if pos_before is None:
            logger.warning("EXIT_NO_POS_SNAPSHOT symbol=%s setup=%s kind=%s",
                           symbol, a.setup, a.kind)
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
        # MySQL: archive completed trade
        if mysql_store is not None:
            try:
                mysql_store.position_closed(
                    symbol=symbol, setup_name=pos_before.setup,
                    exit_px=a.price,
                    close_reason=a.kind, closed_at=last_bar.ts,
                )
            except Exception as exc:
                logger.error("MYSQL_CLOSE_FAILED symbol=%s setup=%s: %s",
                             symbol, pos_before.setup, exc, exc_info=True)
        update_strategy_performance_file(pos_before.setup, pnl, r_realized)
        logger.info("POSITION_CLOSED symbol=%s setup=%s reason=%s exit=%.4f r=%.2f pnl=%.2f",
                    symbol, pos_before.setup, a.kind, a.price, r_realized, pnl)
    return recorded


@dataclass
class VWAPWaveEngine:
    """One-tick bar-close orchestrator.

    Phase A — manage open positions: ingest each fresh bar into the symbol's
    SessionContext, run PositionManager.on_bar (which checks ALL positions for
    that symbol), and route resulting actions through OrderExecutor.

    Phase B — detect new entries: for each symbol's setups, run check(ctx),
    evaluate via RiskManager, and submit through OrderExecutor.

    Supports multiple setups trading the same symbol — each setup's position
    is tracked independently via (symbol, setup) keys in PositionBook.
    """

    symbols: list[tuple[str, str]]
    contexts: dict[str, SessionContext]
    setups: dict[str, list[BaseSetup]]
    risk_manager: RiskManager
    executor: OrderExecutor
    book: PositionBook
    ledger: DailyLedger
    position_manager: PositionManager
    mysql_store: object | None = None

    def tick(self, now: datetime, fresh_bars: dict[str, list[Bar]]) -> None:
        total_bars = sum(len(v) for v in fresh_bars.values())
        logger.info("CYCLE_TICK ts=%s symbols_with_bars=%d total_bars=%d",
                    now.isoformat(), len(fresh_bars), total_bars)
        # Reset per-cycle book of just-closed symbols before Phase A so exits
        # this tick can gate re-entries in Phase B.
        self.book.clear_just_exited()

        # Phase A: ingest bars + manage open positions
        for symbol, asset_class in self.symbols:
            new_bars = fresh_bars.get(symbol) or []
            for bar in new_bars:
                self.contexts[symbol].ingest(bar)

                # Snapshot ALL positions before PM mutates the book
                positions_before = {
                    p.setup: p for p in self.book.get_all(symbol)
                }
                actions = self.position_manager.on_bar(symbol, bar)
                if actions:
                    self._record_exits(
                        symbol, actions, bar,
                        positions_snapshot=positions_before,
                    )
                    parent_order_id = (
                        positions_before[actions[0].setup].order_id
                        if actions[0].setup in positions_before
                        else None
                    )
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
                logger.info("SIGNAL_FIRED symbol=%s setup=%s side=%s entry=%.4f stop=%.4f target=%.4f",
                            signal.symbol, signal.setup, signal.side,
                            signal.entry, signal.stop, signal.target)
                self.executor.submit(signal, decision, asset_class)

    def _record_exits(self, symbol: str, actions: list[PositionAction],
                      last_bar: Bar,
                      positions_snapshot: dict[str, OpenPosition] | None = None,
                      ) -> None:
        record_exits_to_ledger(self.ledger, symbol, actions, last_bar,
                              mysql_store=self.mysql_store,
                              positions_snapshot=positions_snapshot)


def update_strategy_performance_file(setup_name: str, pnl: float, r_realized: float) -> None:
    import json
    import os
    path = "runtime/strategy_performance.json"
    if not os.path.exists(path):
        return
    try:
        with open(path, "r") as f:
            data = json.load(f)
    except Exception:
        return

    if setup_name not in data:
        data[setup_name] = {
            "total_trades": 0, "wins": 0, "losses": 0, "win_rate": 0.0,
            "total_pnl": 0.0, "total_r_realized": 0.0, "avg_pnl": 0.0,
            "avg_r_realized": 0.0, "max_win": 0.0, "max_loss": 0.0
        }

    stats = data[setup_name]
    stats["total_trades"] += 1
    if pnl > 0:
        stats["wins"] += 1
    else:
        stats["losses"] += 1

    stats["win_rate"] = stats["wins"] / stats["total_trades"]
    stats["total_pnl"] += pnl
    stats["total_r_realized"] += r_realized
    stats["avg_pnl"] = stats["total_pnl"] / stats["total_trades"]
    stats["avg_r_realized"] = stats["total_r_realized"] / stats["total_trades"]
    stats["max_win"] = max(stats["max_win"], pnl)
    stats["max_loss"] = min(stats["max_loss"], pnl)

    try:
        with open(path, "w") as f:
            json.dump(data, f, indent=4)
    except Exception:
        pass