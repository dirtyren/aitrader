from __future__ import annotations
import logging
from dataclasses import dataclass
from typing import Callable, Optional

from core.bar import Bar
from state.position_book import OpenPosition, PositionBook


logger = logging.getLogger(__name__)


# Statuses Alpaca returns for an order that has put qty into the account.
# Anything else (new/accepted/held/pending_new/etc.) is "broker has not
# filled this yet" — the engine must NOT virtually exit on it.
_FILLED_STATUSES = frozenset({"filled", "partially_filled"})


@dataclass(frozen=True, slots=True)
class PositionAction:
    symbol: str
    setup: str           # which strategy owned this position
    kind: str            # "stop" | "target" | "time_stop" | "breakeven"
    price: float
    qty: float
    side: str            # side of the open position being acted on


class PositionManager:
    """Per-bar position manager — checks stop/target/breakeven/time_stop.

    With multi-setup positions per symbol, on_bar now processes ALL positions
    for the given symbol at once, each with its own stop/target/breakeven
    thresholds.
    """

    def __init__(
        self, book: PositionBook, max_hold_bars: int, breakeven_at_R: float,
        order_status_for: Optional[Callable[[OpenPosition], Optional[str]]] = None,
        on_fill_confirmed: Optional[Callable[[OpenPosition], None]] = None,
    ) -> None:
        self._book = book
        self._max_hold_bars = max_hold_bars
        self._breakeven_at_R = breakeven_at_R
        # order_status_for(pos) returns the broker order status string, or
        # None if the lookup failed. Used by the fill-confirmation gate to
        # decide whether the engine may act on virtual exit checks.
        # Default returns None — equivalent to "lookup unavailable" — which
        # makes the gate skip the bar; suitable for unit tests that don't
        # need the gate.
        self._order_status_for = order_status_for or (lambda pos: None)
        # on_fill_confirmed(pos) is invoked once per position the first time
        # the broker confirms the fill, so MySQLStore can persist the flag.
        self._on_fill_confirmed = on_fill_confirmed or (lambda pos: None)

    def on_bar(self, symbol: str, bar: Bar) -> list[PositionAction]:
        """Check ALL positions for *symbol* against *bar*.

        Returns actions for any positions whose stop/target/time_stop are
        triggered. The caller (VWAPWaveEngine) handles closing the book entry
        and executing the action.

        Positions whose fill has not been confirmed at the broker are
        silently skipped — without this gate, an unfilled limit-bracket
        whose stop_px got grazed by a low bar would emit a phantom 'stop'
        action and the scheduler would write a fake -PnL trade row.
        """
        all_actions: list[PositionAction] = []
        for pos in self._book.get_all(symbol):
            if not self._confirm_fill(pos):
                # Broker hasn't filled yet — defer everything. bars_held is
                # NOT incremented either, so time_stop doesn't fire on a
                # never-existed position.
                continue
            if pos.exit_submitted:
                # Engine has already submitted (or registered as in-flight)
                # a broker close for this position. Defer everything until
                # the reconciler closes the MySQL row from the broker fill
                # and the next cycle's book reload drops it. bars_held is
                # NOT incremented — same shape as the fill gate.
                continue
            actions = self._check_position(pos, bar)
            all_actions.extend(actions)
        return all_actions

    def _confirm_fill(self, pos: OpenPosition) -> bool:
        """Return True if pos.fill_confirmed; otherwise poll the broker once.

        Sets pos.fill_confirmed=True and invokes on_fill_confirmed (so the
        flag can be persisted) when the broker reports a filled status.
        """
        if pos.fill_confirmed:
            return True
        try:
            status = self._order_status_for(pos)
        except Exception as exc:
            logger.warning(
                "FILL_CONFIRM_LOOKUP_FAILED symbol=%s setup=%s order_id=%s error=%s",
                pos.symbol, pos.setup, pos.order_id, exc,
            )
            return False
        if status is None:
            return False
        if status not in _FILLED_STATUSES:
            logger.info(
                "FILL_CONFIRM_PENDING symbol=%s setup=%s order_id=%s status=%s",
                pos.symbol, pos.setup, pos.order_id, status,
            )
            return False
        pos.fill_confirmed = True
        try:
            self._on_fill_confirmed(pos)
        except Exception as exc:
            logger.warning(
                "FILL_CONFIRM_PERSIST_FAILED symbol=%s setup=%s error=%s",
                pos.symbol, pos.setup, exc,
            )
        logger.info(
            "FILL_CONFIRMED symbol=%s setup=%s order_id=%s status=%s",
            pos.symbol, pos.setup, pos.order_id, status,
        )
        return True

    def _check_position(self, pos: OpenPosition, bar: Bar) -> list[PositionAction]:
        """Check a single position and return triggered actions.

        Side-effect: mutates pos (bars_held, breakeven_moved, stop_px) and
        closes the book entry on exit kinds.
        """
        if pos.adopted:
            pos.bars_held += 1
            # Adopted positions still need stop/target protection.
            # Skip breakeven and time_stop (no entry risk context), but
            # DO check stop-loss and take-profit levels.
            if pos.side == "long":
                if pos.stop_px is not None and bar.low <= pos.stop_px:
                    actions = [self._exit(pos, "stop", pos.stop_px)]
                    self._book.close(pos.symbol, pos.setup)
                    return actions
                if pos.target_px is not None and bar.high >= pos.target_px:
                    actions = [self._exit(pos, "target", pos.target_px)]
                    self._book.close(pos.symbol, pos.setup)
                    return actions
            else:  # short
                if pos.stop_px is not None and bar.high >= pos.stop_px:
                    actions = [self._exit(pos, "stop", pos.stop_px)]
                    self._book.close(pos.symbol, pos.setup)
                    return actions
                if pos.target_px is not None and bar.low <= pos.target_px:
                    actions = [self._exit(pos, "target", pos.target_px)]
                    self._book.close(pos.symbol, pos.setup)
                    return actions
            return []

        actions: list[PositionAction] = []

        if pos.side == "long":
            if pos.stop_px is not None and bar.low <= pos.stop_px:
                actions.append(self._exit(pos, "stop", pos.stop_px))
                self._book.close(pos.symbol, pos.setup)
                return actions
            if pos.target_px is not None and bar.high >= pos.target_px:
                actions.append(self._exit(pos, "target", pos.target_px))
                self._book.close(pos.symbol, pos.setup)
                return actions
            if not pos.breakeven_moved and pos.risk_per_share > 0:
                trigger = pos.entry_px + self._breakeven_at_R * pos.risk_per_share
                if bar.high >= trigger:
                    pos.stop_px = pos.entry_px
                    pos.breakeven_moved = True
                    actions.append(self._exit(pos, "breakeven", pos.entry_px))
        else:  # short
            if pos.stop_px is not None and bar.high >= pos.stop_px:
                actions.append(self._exit(pos, "stop", pos.stop_px))
                self._book.close(pos.symbol, pos.setup)
                return actions
            if pos.target_px is not None and bar.low <= pos.target_px:
                actions.append(self._exit(pos, "target", pos.target_px))
                self._book.close(pos.symbol, pos.setup)
                return actions
            if not pos.breakeven_moved and pos.risk_per_share > 0:
                trigger = pos.entry_px - self._breakeven_at_R * pos.risk_per_share
                if bar.low <= trigger:
                    pos.stop_px = pos.entry_px
                    pos.breakeven_moved = True
                    actions.append(self._exit(pos, "breakeven", pos.entry_px))

        pos.bars_held += 1
        if pos.bars_held > self._max_hold_bars:
            actions.append(self._exit(pos, "time_stop", bar.close))
            self._book.close(pos.symbol, pos.setup)

        return actions

    @staticmethod
    def _exit(pos: OpenPosition, kind: str, price: float) -> PositionAction:
        return PositionAction(
            symbol=pos.symbol, setup=pos.setup,
            kind=kind, price=price, qty=pos.qty, side=pos.side,
        )