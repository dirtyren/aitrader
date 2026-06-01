"""Cross-strategy invariant checker.

The invariant: for every symbol, Σ (open MySQL qty across all strategies)
== broker qty for that symbol. Anomalies group into three directions defined
by the spec.

This module is pure: given a MySQL session and a broker positions snapshot,
it returns a list of Anomaly records. It does not mutate.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from state.mysql_store import MySQLStore, PositionRow


@dataclass(frozen=True)
class Anomaly:
    direction: str  # 'qty_drift' | 'mysql_only' | 'broker_only'
    symbol: str
    strategy_id: int | None
    snapshot: dict[str, Any]
    # The reconciler instance's asset_class — stamped onto StrikeRow /
    # EventRow rows so the dashboard subtabs and parallel reconcilers stay
    # scoped to their own lane. None on legacy/test paths only.
    asset_class: str | None = None

    @property
    def key(self) -> str:
        if self.direction == "mysql_only":
            return f"mysql_only:{self.strategy_id}:{self.symbol}"
        return f"{self.direction}:{self.symbol}"


def _normalize(symbol: str) -> str:
    return symbol.replace("/", "")


def check_invariant(
    session: Session,
    store: MySQLStore,
    broker_qty_by_symbol: dict[str, float],
    *,
    qty_eps: float,
    asset_class: str | None = None,
) -> list[Anomaly]:
    """Compare MySQL open-position state to the broker snapshot.

    Args:
        session: live SQLAlchemy session (used to query PositionRow directly
            for per-strategy listings; sum_qty_by_symbol does its own query).
        store: MySQLStore — used for sum_qty_by_symbol().
        broker_qty_by_symbol: {symbol → qty} from Alpaca's get_positions, with
            symbols already normalized to broker-flat form.
        qty_eps: tolerance for floating-point comparison.
        asset_class: when set, scopes the MySQL aggregate AND the per-strategy
            mysql_only listing to that side. The broker side is already
            scoped by the caller (each reconciler holds its own per-class
            AlpacaClient). Stamped on every emitted Anomaly so the strikes
            layer can persist it.

    Returns:
        list of Anomaly records — empty if the invariant holds.
    """
    broker_norm = {_normalize(s): q for s, q in broker_qty_by_symbol.items()}
    mysql_sums = store.sum_qty_by_symbol(asset_class=asset_class)

    anomalies: list[Anomaly] = []

    # qty_drift: symbol present in BOTH but sums differ.
    for symbol in set(mysql_sums) & set(broker_norm):
        m, b = mysql_sums[symbol], broker_norm[symbol]
        if abs(m - b) > qty_eps:
            anomalies.append(Anomaly(
                direction="qty_drift",
                symbol=symbol,
                strategy_id=None,
                snapshot={"mysql_sum": m, "broker_qty": b},
                asset_class=asset_class,
            ))

    # mysql_only: open in MySQL, no broker position for that symbol.
    mysql_only_symbols = set(mysql_sums) - set(broker_norm)
    if mysql_only_symbols:
        q = session.query(
            PositionRow.strategy_id, PositionRow.symbol, PositionRow.qty,
        ).filter(PositionRow.status == "open")
        if asset_class is not None:
            q = q.filter(PositionRow.asset_class == asset_class)
        for strategy_id, raw_symbol, qty in q.all():
            sym = _normalize(raw_symbol)
            if sym not in mysql_only_symbols:
                continue
            anomalies.append(Anomaly(
                direction="mysql_only",
                symbol=sym,
                strategy_id=strategy_id,
                snapshot={"mysql_qty": float(qty), "broker_qty": 0.0},
                asset_class=asset_class,
            ))

    # broker_only: broker position for a symbol, no open MySQL rows.
    for symbol in set(broker_norm) - set(mysql_sums):
        anomalies.append(Anomaly(
            direction="broker_only",
            symbol=symbol,
            strategy_id=None,
            snapshot={"mysql_sum": 0.0, "broker_qty": broker_norm[symbol]},
            asset_class=asset_class,
        ))

    return anomalies
