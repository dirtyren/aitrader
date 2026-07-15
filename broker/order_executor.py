from __future__ import annotations
import logging
from typing import Optional

from broker.alpaca_client import InsufficientBuyingPowerError, OrderRejectedError
from broker.safe_close import submit_close_with_drift_recovery
from core.position_manager import PositionAction
from state.position_book import OpenPosition, PositionBook
from strategies.base_setup import SetupSignal
from risk.manager import RiskDecision
from notifications import send_position_open_alert
from broker.client_order_id import Role, make_client_order_id

logger = logging.getLogger(__name__)

# Alpaca error fragments that indicate a breakeven replace is benign:
#  - "must be (>=|<=) base_price ± 0.01": stop drifted past current quote
#    between bar-close decision and PATCH; original bracket stop still protects.
#  - "order is not open": bracket child already filled or canceled.
#  - "already replaced": broker has already accepted a prior replace for this
#    leg (seen in COIN logs: BREAKEVEN_REPLACE_FAILED looping every cycle).
_BENIGN_BREAKEVEN_FRAGMENTS = (
    "must be >= base_price",
    "must be <= base_price",
    "order is not open",
    "already replaced",
)


def _tick_for(price: float) -> float:
    """Same tick model as AlpacaClient._round_to_tick — $0.01 at or above
    $1, $0.0001 below. Used by the bracket-geometry validator only; the
    actual rounding still happens at the Alpaca layer."""
    return 0.01 if price >= 1 else 0.0001


def _bracket_levels_ok(
    side: str, entry: float, stop: float, target: float,
) -> tuple[bool, str]:
    """Enforce Alpaca's bracket geometry up front so a setup bug fails
    locally instead of round-tripping as a 422 from the broker.

    Long bracket: stop <= entry-tick AND target >= entry+tick.
    Short bracket: stop >= entry+tick AND target <= entry-tick.
    """
    tick = _tick_for(entry)
    if side == "long":
        if stop > entry - tick:
            return False, (
                f"long stop must be <= entry-{tick:g}; "
                f"got entry={entry} stop={stop}"
            )
        if target < entry + tick:
            return False, (
                f"long target must be >= entry+{tick:g}; "
                f"got entry={entry} target={target}"
            )
    else:  # short
        if stop < entry + tick:
            return False, (
                f"short stop must be >= entry+{tick:g}; "
                f"got entry={entry} stop={stop}"
            )
        if target > entry - tick:
            return False, (
                f"short target must be <= entry-{tick:g}; "
                f"got entry={entry} target={target}"
            )
    return True, ""


class OrderExecutor:
    """Translates an approved SetupSignal+RiskDecision into broker orders."""

    def __init__(self, alpaca_client, book: PositionBook,
                 strategy_name: str,
                 logger: logging.Logger | None = None,
                 mysql_store=None):
        if not strategy_name:
            raise ValueError("OrderExecutor requires a non-empty strategy_name")
        self.client = alpaca_client
        self.book = book
        self.strategy_name = strategy_name
        self.logger = logger or logging.getLogger("vwap_wave.executor")
        self._dtbp_exhausted = False
        self._mysql = mysql_store

    def reset_cycle(self) -> None:
        """Clear per-cycle short-circuit flags. Call at the top of each main-loop tick."""
        self._dtbp_exhausted = False

    @staticmethod
    def _alpaca_side(side: str) -> str:
        return "buy" if side == "long" else "sell"

    @staticmethod
    def _extract_stop_leg_id(bracket_response: dict) -> str | None:
        """Find the stop_loss child id in a bracket order response.

        Alpaca returns children under `legs`; the stop leg has `stop_price` set
        (and typically type "stop" / "stop_limit"). Match by stop_price presence
        for resilience against minor schema variations.
        """
        for leg in bracket_response.get("legs") or []:
            if leg.get("stop_price") is not None or leg.get("type") in ("stop", "stop_limit"):
                return leg.get("id")
        return None

    def submit(self, signal: SetupSignal, decision: RiskDecision,
               asset_class: str) -> Optional[OpenPosition]:
        if not decision.approved:
            self.logger.info("ORDER_REJECTED symbol=%s reason=%s",
                             signal.symbol, decision.reason)
            return None

        if self.book.was_just_exited(signal.symbol):
            self.logger.info("ORDER_SKIPPED_RECENTLY_EXITED symbol=%s setup=%s",
                             signal.symbol, signal.setup)
            return None

        # Pending or open position from this strategy on the same symbol —
        # blocks both opposite-side double-entries (which Alpaca rejects
        # with `cannot open a short sell while a long buy order is open`)
        # and same-(symbol, setup) duplicates (which book.add would raise
        # on later). The book gets rebuilt from MySQL each cycle, so a
        # reconciler-resolved phantom row clears this guard automatically.
        existing = self.book.get_all(signal.symbol)
        if existing:
            existing_setups = ",".join(p.setup for p in existing)
            existing_sides = sorted({p.side for p in existing})
            self.logger.info(
                "ORDER_SKIPPED_OPPOSING_OPEN_ORDER symbol=%s new_setup=%s "
                "new_side=%s existing_setups=%s existing_sides=%s",
                signal.symbol, signal.setup, signal.side,
                existing_setups, existing_sides,
            )
            return None

        if asset_class == "equity" and self._dtbp_exhausted:
            self.logger.info("ORDER_SKIPPED_DTBP_EXHAUSTED symbol=%s setup=%s",
                             signal.symbol, signal.setup)
            return None

        if asset_class == "crypto" and signal.side == "short":
            self.logger.info("ORDER_SKIPPED_CRYPTO_SHORT symbol=%s setup=%s",
                             signal.symbol, signal.setup)
            return None

        # Bracket geometry: only equity uses Alpaca-side brackets. Crypto
        # exits are engine-virtual; pre-market extended_hours equity skips
        # the bracket here and attaches the OCO post-open. So we validate
        # the regular-session equity path only — that's where Alpaca
        # would otherwise reject with `take_profit.limit_price must be
        # <= base_price - 0.01` and similar.
        extended_hours_signal = bool(signal.notes.get("extended_hours"))
        if asset_class == "equity" and not extended_hours_signal:
            ok, reason = _bracket_levels_ok(
                signal.side, signal.entry, signal.stop, signal.target,
            )
            if not ok:
                self.logger.warning(
                    "ORDER_SKIPPED_INVALID_LEVELS symbol=%s setup=%s side=%s "
                    "entry=%.4f stop=%.4f target=%.4f reason=%s",
                    signal.symbol, signal.setup, signal.side,
                    signal.entry, signal.stop, signal.target, reason,
                )
                return None

        alp_side = self._alpaca_side(signal.side)
        entry_coid = make_client_order_id(
            self.strategy_name, signal.setup, signal.symbol, Role.ENTRY,
        )

        extended_hours = bool(signal.notes.get("extended_hours"))

        try:
            oco_order = None  # Only set in regular equity; extended_hours uses post-open attach
            if asset_class == "equity" and extended_hours:
                # Pre-market entry: plain limit with extended_hours=True. The
                # OCO bracket is attached after the regular session opens.
                order = self.client.submit_order(
                    symbol=signal.symbol,
                    qty=decision.qty,
                    side=alp_side,
                    order_type="limit",
                    time_in_force="day",
                    limit_price=signal.entry,
                    client_order_id=entry_coid,
                    extended_hours=True,
                )
            elif asset_class == "equity":
                # Market entry — fills immediately instead of the old
                # limit-bracket that went unfilled in fast-moving
                # breakouts (ORB, VWAP Wave, etc.), causing the
                # reconciled_gone / entry_never_filled $0-PnL epidemic.
                order = self.client.submit_order(
                    symbol=signal.symbol,
                    qty=decision.qty,
                    side=alp_side,
                    order_type="market",
                    time_in_force="day",
                    client_order_id=entry_coid,
                )
                # Attach OCO stop/target bracket to the filled position
                oco_coid = make_client_order_id(
                    self.strategy_name, signal.setup, signal.symbol, Role.STOP,
                )
                exit_side = "sell" if alp_side == "buy" else "buy"
                oco_order = None
                try:
                    oco_order = self.client.attach_oco(
                        symbol=signal.symbol,
                        qty=decision.qty,
                        side=exit_side,
                        stop_price=signal.stop,
                        target_price=signal.target,
                        time_in_force="day",
                        client_order_id=oco_coid,
                    )
                except Exception as exc:
                    self.logger.warning(
                        "OCO_ATTACH_FAILED_AFTER_MARKET_ENTRY symbol=%s "
                        "setup=%s error=%s",
                        signal.symbol, signal.setup, exc,
                    )
            elif asset_class == "crypto":
                # Crypto: market entry + engine-managed virtual stop/target
                order = self.client.submit_order(
                    symbol=signal.symbol,
                    qty=decision.qty,
                    side=alp_side,
                    order_type="market",
                    time_in_force="gtc",
                    client_order_id=entry_coid,
                )
            else:
                raise ValueError(f"Unknown asset_class: {asset_class}")
        except InsufficientBuyingPowerError as exc:
            if asset_class == "equity":
                self._dtbp_exhausted = True
            self.logger.warning(
                "ORDER_REJECTED_DTBP symbol=%s setup=%s qty=%s notional=%.2f detail=%s",
                signal.symbol, signal.setup, decision.qty,
                decision.qty * signal.entry, exc.message,
            )
            return None
        except Exception as exc:
            self.logger.error("ORDER_SUBMIT_FAILED symbol=%s error=%s",
                              signal.symbol, exc, exc_info=True)
            return None

        stop_order_id = self._extract_stop_leg_id(oco_order) if (asset_class == "equity" and oco_order) else None
        # Crypto: target is engine-virtual. Submitting an immediate limit TP
        # right after a market entry trips Alpaca's wash-trade detector
        # ("potential wash trade detected. use complex orders") — and when
        # the TP submit fails, handle_actions used to skip the broker close
        # on virtual target hits, leaving phantom broker positions that ate
        # buying power. Stops are already virtual; making target virtual too
        # keeps the crypto path consistent and removes both failure modes.
        target_order_id = None

        # Market orders fill synchronously — when Alpaca returns
        # status='filled' in the submit response, the broker already owns
        # the qty and we can let the engine act on virtual exits right
        # away.
        order_status = (order or {}).get("status") if isinstance(order, dict) else None
        fill_confirmed = order_status in ("filled", "partially_filled")

        # Use the actual Alpaca fill price when available (market orders
        # fill synchronously and return filled_avg_price). Fall back to
        # signal.entry for limit orders that haven't filled yet (extended
        # hours) — the fill is confirmed later by the reconciler.
        actual_fill = float(order.get("filled_avg_price") or 0)
        entry_px = actual_fill if actual_fill > 0 else signal.entry

        pos = OpenPosition(
            symbol=signal.symbol, setup=signal.setup, side=signal.side,
            qty=decision.qty, entry_px=entry_px, stop_px=signal.stop,
            target_px=signal.target, opened_at=signal.ts,
            order_id=order.get("id", ""),
            stop_order_id=stop_order_id,
            target_order_id=target_order_id,
            initial_stop_px=signal.stop,
            client_order_id=entry_coid,
            pending_oco_attach=extended_hours,
            fill_confirmed=fill_confirmed,
        )
        self.book.add(pos)
        # Persist to MySQL so position metadata survives container restarts
        if self._mysql is not None:
            try:
                self._mysql.position_opened(pos, asset_class)
            except Exception as exc:
                self.logger.error("MYSQL_SAVE_FAILED symbol=%s: %s",
                                  signal.symbol, exc, exc_info=True)
        # Notify Telegram
        send_position_open_alert(
            strategy_name=self.strategy_name,
            symbol=pos.symbol,
            side=pos.side,
            qty=pos.qty,
            entry_px=pos.entry_px,
            stop_px=pos.stop_px,
            target_px=pos.target_px,
            setup_name=pos.setup,
            asset_class=asset_class,
        )
        self.logger.info("ORDER_SUBMITTED setup=%s symbol=%s side=%s qty=%s "
                         "entry=%.4f stop=%.4f target=%.4f order_id=%s",
                         signal.setup, signal.symbol, signal.side, decision.qty,
                         signal.entry, signal.stop, signal.target, order.get("id"))
        return pos

    def close_position(
        self, symbol: str, side: str, qty: float,
        *,
        setup: str,
        asset_class: str = "crypto",
    ) -> dict | None:
        """Submit a market close order. Used for virtual / time stops.

        ``setup`` is required so the exit COID parses back to the
        (strategy, setup, symbol) triple at reconciler/fills.py
        :apply_tagged_fill — without it, the reconciler can't match the
        close fill to the open row and the row stays open indefinitely.
        See incident 2026-06-02 (COIN: 22 stacked broker positions vs 1
        open MySQL row) and design doc 2026-06-02-engine-exit-idempotency.

        ``asset_class`` controls the fee-drift safety margin: crypto closes
        shave ~1e-6 off the requested qty (fees drain from the asset side
        between snapshot and submit), equity passes through unchanged.
        """
        exit_coid = make_client_order_id(
            self.strategy_name, setup, symbol, Role.EXIT,
        )
        return submit_close_with_drift_recovery(
            client=self.client,
            symbol=symbol,
            qty=qty,
            side="sell" if side == "long" else "buy",
            client_order_id=exit_coid,
            asset_class=asset_class,
        )

    def handle_actions(self, actions: list[PositionAction],
                       asset_class: str,
                       parent_order_id: str | None = None) -> None:
        """Dispatch PositionManager actions to the broker.

        Equity bracket: the broker owns stop/target server-side, so we skip
        those. On time_stop we cancel the bracket parent (which kills its OCO
        children) and then market-close the position. Breakeven for equity
        would require replacing the bracket child stop — not yet implemented,
        logged only.

        Crypto: no broker-side stop/target, so every exit kind translates to a
        market close. Breakeven is engine-state only.
        """
        for a in actions:
            if a.kind == "breakeven":
                if asset_class == "equity":
                    self._move_equity_stop_to_breakeven(a)
                else:
                    self.logger.info("BREAKEVEN symbol=%s side=%s entry=%.4f",
                                     a.symbol, a.side, a.price)
                continue

            if asset_class == "equity":
                if a.kind in ("stop", "target"):
                    self._mark_exit_submitted(a.symbol, a.setup)
                    self.logger.info(
                        "BRACKET_EXIT symbol=%s kind=%s price=%.4f setup=%s",
                        a.symbol, a.kind, a.price, a.setup,
                    )
                    continue
                if a.kind == "time_stop":
                    if parent_order_id:
                        try:
                            self.client.cancel_order(parent_order_id)
                        except Exception as exc:
                            self.logger.warning(
                                "CANCEL_FAILED_DURING_TIME_STOP symbol=%s "
                                "order_id=%s error=%s — treating parent as "
                                "already terminal, proceeding with close",
                                a.symbol, parent_order_id, exc,
                            )
                    close_result = self.close_position(a.symbol, a.side, a.qty,
                                                       setup=a.setup,
                                                       asset_class="equity")
                    if close_result is not None:
                        self._mark_exit_submitted(a.symbol, a.setup)
                    self.logger.info(
                        "TIME_STOP symbol=%s side=%s qty=%s setup=%s",
                        a.symbol, a.side, a.qty, a.setup,
                    )
                    continue

            elif asset_class == "crypto":
                if a.kind in ("stop", "target", "time_stop"):
                    # Crypto exits are fully engine-managed: stop, target, and
                    # time_stop all translate to a market close on the broker.
                    pos = self.book.get(a.symbol, a.setup)
                    if pos and getattr(pos, "target_order_id", None):
                        # Legacy adopted positions may still carry a TP id —
                        # cancel before closing to keep the broker side tidy.
                        try:
                            self.client.cancel_order(pos.target_order_id)
                        except Exception as exc:
                            self.logger.error(
                                "CANCEL_TP_FAILED symbol=%s order_id=%s error=%s",
                                a.symbol, pos.target_order_id, exc,
                            )
                    close_result = self.close_position(
                        a.symbol, a.side, a.qty,
                        setup=a.setup, asset_class="crypto",
                    )
                    if close_result is not None:
                        self._mark_exit_submitted(a.symbol, a.setup)
                    self.logger.info(
                        "VIRTUAL_EXIT symbol=%s kind=%s price=%.4f qty=%s setup=%s",
                        a.symbol, a.kind, a.price, a.qty, a.setup,
                    )
                    continue

            self.logger.warning("UNHANDLED_ACTION symbol=%s kind=%s asset_class=%s",
                                a.symbol, a.kind, asset_class)

    def _mark_exit_submitted(self, symbol: str, setup: str) -> None:
        """Flip exit_submitted=True on the in-memory book and persist to
        MySQL so PositionManager.on_bar stops emitting further exits for
        this position. Idempotent — safe to call repeatedly.
        """
        pos = self.book.get(symbol, setup)
        if pos is not None:
            pos.exit_submitted = True
        if self._mysql is not None:
            try:
                self._mysql.mark_exit_submitted(
                    strategy_id=self._mysql.strategy_id,
                    symbol=symbol, setup_name=setup,
                )
            except Exception as exc:
                self.logger.error(
                    "MARK_EXIT_SUBMITTED_FAILED symbol=%s setup=%s error=%s",
                    symbol, setup, exc, exc_info=True,
                )

    def _move_equity_stop_to_breakeven(self, a: PositionAction) -> None:
        pos = self.book.get(a.symbol, a.setup)
        stop_leg = pos.stop_order_id if pos else None
        if not stop_leg:
            self.logger.warning("BREAKEVEN_NO_STOP_LEG symbol=%s — skipping replace", a.symbol)
            return
        try:
            self.client.replace_order(stop_leg, stop_price=a.price)
            if pos is not None:
                pos.breakeven_moved = True
            self.logger.info("BREAKEVEN_REPLACED symbol=%s stop_leg=%s new_stop=%.4f",
                             a.symbol, stop_leg, a.price)
        except OrderRejectedError as exc:
            msg = str(exc)
            if any(frag in msg for frag in _BENIGN_BREAKEVEN_FRAGMENTS):
                # Broker has already replaced the leg (or won't accept the
                # replace because the order is closed / too close to quote).
                # Flag the move as done so PositionManager stops re-emitting
                # the breakeven action — this is the parallel idempotency
                # hole to exit_submitted (today's COIN log: 6 retries before
                # the position even time-stopped).
                if pos is not None:
                    pos.breakeven_moved = True
                self.logger.warning("BREAKEVEN_SKIPPED symbol=%s stop_leg=%s reason=%s",
                                    a.symbol, stop_leg, msg)
                return
            self.logger.error("BREAKEVEN_REPLACE_FAILED symbol=%s stop_leg=%s error=%s",
                              a.symbol, stop_leg, exc, exc_info=True)
        except Exception as exc:
            self.logger.error("BREAKEVEN_REPLACE_FAILED symbol=%s stop_leg=%s error=%s",
                              a.symbol, stop_leg, exc, exc_info=True)
