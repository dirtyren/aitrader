"""Per-strategy position tracking — one book per strategy process.

Architecture:
    Multiple setups (e.g., vwap_wave, vwap_bands) may trade the same symbol
    within a single strategy container. The book key is (symbol, setup), not
    just symbol, so each setup's position is tracked independently with its
    own qty, stop, and target levels.

    The broker (Alpaca) aggregates all positions on the account, so the
    reconciler logs drift between book qty and broker qty without correcting.
    Each setup's position is managed independently by PositionManager.
"""

from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime


@dataclass
class OpenPosition:
    symbol: str
    setup: str               # strategy/setup name (e.g., "vwap_wave", "vwap_bands")
    side: str                # "long" | "short"
    qty: float
    entry_px: float
    stop_px: float | None
    target_px: float | None
    opened_at: datetime
    order_id: str
    breakeven_moved: bool = False
    bars_held: int = 0
    stop_order_id: str | None = None      # bracket stop-leg id (equity); None for crypto
    target_order_id: str | None = None    # limit order id for crypto TP
    initial_stop_px: float | None = None  # original stop at entry; survives breakeven moves
    adopted: bool = False                 # True for positions reconciled from broker

    @property
    def initial_risk_per_share(self) -> float:
        ref = self.initial_stop_px if self.initial_stop_px is not None else self.stop_px
        if ref is None:
            return 0.0
        return abs(self.entry_px - ref)

    @property
    def risk_per_share(self) -> float:
        if self.stop_px is None:
            return 0.0
        return abs(self.entry_px - self.stop_px)

    @property
    def open_risk_usd(self) -> float:
        return self.risk_per_share * self.qty

    @property
    def key(self) -> tuple[str, str]:
        """Unique key: (symbol, setup)."""
        return (self.symbol, self.setup)


class PositionBook:
    """Multi-position book — one entry per (symbol, setup) pair."""

    def __init__(self) -> None:
        # Key: (symbol, setup) -> OpenPosition
        self._positions: dict[tuple[str, str], OpenPosition] = {}
        # Set of (symbol, setup) that closed during the current cycle.
        self._just_exited: set[tuple[str, str]] = set()

    # ── Lookup ──────────────────────────────────────────────────────────

    def get(self, symbol: str, setup: str | None = None) -> OpenPosition | None:
        """Return position for exact (symbol, setup), or first for symbol only.

        When setup is None and multiple positions exist, returns the first one
        (dict insertion order) for backward-compatible callers like the snapshot
        collector. Prefer get_all() for complete results.
        """
        sym_norm = symbol.replace("/", "")
        if setup is not None:
            for (sym, set_name), pos in self._positions.items():
                if sym.replace("/", "") == sym_norm and set_name == setup:
                    return pos
            return None
        for (sym, _), pos in self._positions.items():
            if sym.replace("/", "") == sym_norm:
                return pos
        return None

    def get_all(self, symbol: str) -> list[OpenPosition]:
        """Return ALL positions for this symbol (one per setup)."""
        sym_norm = symbol.replace("/", "")
        return [pos for (sym, _), pos in self._positions.items() if sym.replace("/", "") == sym_norm]

    def has_symbol(self, symbol: str) -> bool:
        """True if any position exists for this symbol."""
        sym_norm = symbol.replace("/", "")
        return any(sym.replace("/", "") == sym_norm for (sym, _) in self._positions)

    # ── Mutation ────────────────────────────────────────────────────────

    def add(self, p: OpenPosition) -> None:
        key = p.key
        sym_norm = p.symbol.replace("/", "")
        for k in self._positions:
            if k[0].replace("/", "") == sym_norm and k[1] == p.setup:
                raise ValueError(
                    f"Position already open on {k[0]} for setup {k[1]!r}"
                )
        self._positions[key] = p

    def close(self, symbol: str, setup: str | None = None) -> OpenPosition | None:
        """Close position(s) for a symbol.

        If setup is given, closes just that one. If not, closes ALL positions
        for the symbol and returns the most recently opened one (legacy compat).
        """
        sym_norm = symbol.replace("/", "")
        if setup is not None:
            target_key = None
            for k in self._positions:
                if k[0].replace("/", "") == sym_norm and k[1] == setup:
                    target_key = k
                    break
            if target_key is not None:
                pos = self._positions.pop(target_key, None)
                if pos is not None:
                    self._just_exited.add(target_key)
                return pos
            return None

        # Close all positions for this symbol
        keys = [k for k in self._positions if k[0].replace("/", "") == sym_norm]
        if not keys:
            return None
        last: OpenPosition | None = None
        for k in keys:
            last = self._positions.pop(k)
            self._just_exited.add(k)
        return last

    def was_just_exited(self, symbol: str, setup: str | None = None) -> bool:
        sym_norm = symbol.replace("/", "")
        if setup is not None:
            return any(k[0].replace("/", "") == sym_norm and k[1] == setup for k in self._just_exited)
        return any(k[0].replace("/", "") == sym_norm for k in self._just_exited)

    def clear_just_exited(self) -> None:
        self._just_exited.clear()

    # ── Introspection ───────────────────────────────────────────────────

    def symbols(self) -> list[str]:
        """Unique symbols with at least one open position."""
        seen: set[str] = set()
        for sym, _ in self._positions:
            seen.add(sym)
        return list(seen)

    def count(self) -> int:
        return len(self._positions)

    def all(self) -> list[OpenPosition]:
        return list(self._positions.values())

    def aggregate_open_risk_usd(self) -> float:
        return sum(p.open_risk_usd for p in self._positions.values())