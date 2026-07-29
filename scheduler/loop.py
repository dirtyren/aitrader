from __future__ import annotations
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from broker.order_executor import OrderExecutor
from core.asset_class import is_session_active
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
                           asset_class: str | None = None,
                           ) -> list[TradeRecord]:
    """Append a TradeRecord per stop/target/time_stop action and return them.

    Uses positions_snapshot because PositionManager.on_bar may close the book
    entry on exit kinds. With multi-setup positions, each action references
    the position that triggered it via the *setup* field in PositionAction,
    so we match by setup name when multiple positions exist on the same symbol.

    The intraday DailyLedger always gets a record (so live PnL views update
    the moment the engine sees a stop touch). MySQL is *not* updated here:
    every exit kind we handle now resolves at the broker (equity bracket
    fires the stop/target server-side; time_stop and crypto submit a
    market close), so the authoritative MySQL close arrives via
    `reconciler/fills.py:apply_tagged_fill` with the broker's actual fill
    price and a role-derived close_reason. Writing here would (a) record
    the wrong exit_px (the stop level vs the real fill) and (b) phantom-
    close positions whose entry never actually filled.
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
        update_strategy_performance_file(pos_before.setup, pnl, r_realized)
        logger.info(
            "ENGINE_EXIT_DEFERRED_TO_BROKER_FILL symbol=%s setup=%s "
            "asset_class=%s kind=%s exit_px=%.4f intraday_pnl=%.2f r=%.2f",
            symbol, pos_before.setup, asset_class, a.kind, a.price, pnl, r_realized,
        )
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
    _deferred_signals: list[tuple["SetupSignal", "RiskDecision", str]] = None  # type: ignore[assignment]

    def __post_init__(self):
        self._deferred_signals = []

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
                        asset_class=asset_class,
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

        # ── Deferred-entry processing ──
        # Signals flagged `defer_to_next_bar` from the PREVIOUS tick are
        # submitted NOW — bars have just been ingested for this tick, so
        # the market order fills at the next bar's open price.
        # Guard: if the session has closed between signal generation and now,
        # skip execution — submitting to a closed market causes fill-at-next-open
        # price gaps (e.g. GLW: signal entry $156.31 vs fill $149.28).
        deferred = self._deferred_signals
        self._deferred_signals = []
        now_utc = datetime.now(timezone.utc)
        for signal, decision, asset_class in deferred:
            ctx = self.contexts.get(signal.symbol)
            ac_cfg = ctx.asset_class if ctx else None
            if ac_cfg is not None and not is_session_active(now_utc, ac_cfg):
                logger.info("DEFERRED_SKIPPED_CLOSED symbol=%s setup=%s — session closed",
                            signal.symbol, signal.setup)
                continue
            logger.info("DEFERRED_FIRED symbol=%s setup=%s side=%s entry=%.4f",
                        signal.symbol, signal.setup, signal.side, signal.entry)
            self.executor.submit(signal, decision, asset_class)

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
                if signal.notes.get("defer_to_next_bar"):
                    self._deferred_signals.append((signal, decision, asset_class))
                    logger.info("SIGNAL_DEFERRED symbol=%s setup=%s — will fire at next bar open",
                                signal.symbol, signal.setup)
                else:
                    self.executor.submit(signal, decision, asset_class)

    def _record_exits(self, symbol: str, actions: list[PositionAction],
                      last_bar: Bar,
                      positions_snapshot: dict[str, OpenPosition] | None = None,
                      asset_class: str | None = None,
                      ) -> None:
        record_exits_to_ledger(self.ledger, symbol, actions, last_bar,
                              mysql_store=self.mysql_store,
                              positions_snapshot=positions_snapshot,
                              asset_class=asset_class)


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