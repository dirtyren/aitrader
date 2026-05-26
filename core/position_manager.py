from __future__ import annotations
from dataclasses import dataclass

from core.bar import Bar
from state.position_book import OpenPosition, PositionBook


@dataclass(frozen=True, slots=True)
class PositionAction:
    symbol: str
    kind: str           # "stop" | "target" | "time_stop" | "breakeven"
    price: float
    qty: float
    side: str           # side of the open position being acted on


class PositionManager:
    def __init__(self, book: PositionBook, max_hold_bars: int, breakeven_at_R: float) -> None:
        self._book = book
        self._max_hold_bars = max_hold_bars
        self._breakeven_at_R = breakeven_at_R

    def on_bar(self, symbol: str, bar: Bar) -> list[PositionAction]:
        pos = self._book.get(symbol)
        if pos is None:
            return []

        if pos.adopted:
            pos.bars_held += 1
            # Adopted positions still need stop/target protection —
            # the bug was that we skipped the check entirely (issue 2026-05-26).
            # Skip breakeven and time_stop (no entry risk context), but
            # DO check stop-loss and take-profit levels.
            if pos.side == "long":
                if pos.stop_px is not None and bar.low <= pos.stop_px:
                    actions = [self._exit(pos, "stop", pos.stop_px)]
                    self._book.close(symbol)
                    return actions
                if pos.target_px is not None and bar.high >= pos.target_px:
                    actions = [self._exit(pos, "target", pos.target_px)]
                    self._book.close(symbol)
                    return actions
            else:  # short
                if pos.stop_px is not None and bar.high >= pos.stop_px:
                    actions = [self._exit(pos, "stop", pos.stop_px)]
                    self._book.close(symbol)
                    return actions
                if pos.target_px is not None and bar.low <= pos.target_px:
                    actions = [self._exit(pos, "target", pos.target_px)]
                    self._book.close(symbol)
                    return actions
            return []

        actions: list[PositionAction] = []

        if pos.side == "long":
            if pos.stop_px is not None and bar.low <= pos.stop_px:
                actions.append(self._exit(pos, "stop", pos.stop_px))
                self._book.close(symbol)
                return actions
            if pos.target_px is not None and bar.high >= pos.target_px:
                actions.append(self._exit(pos, "target", pos.target_px))
                self._book.close(symbol)
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
                self._book.close(symbol)
                return actions
            if pos.target_px is not None and bar.low <= pos.target_px:
                actions.append(self._exit(pos, "target", pos.target_px))
                self._book.close(symbol)
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
            self._book.close(symbol)

        return actions

    @staticmethod
    def _exit(pos: OpenPosition, kind: str, price: float) -> PositionAction:
        return PositionAction(symbol=pos.symbol, kind=kind, price=price, qty=pos.qty, side=pos.side)
