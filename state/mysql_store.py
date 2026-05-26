"""MySQL-backed persistence for positions, trades, and strategy state.

Provides durability across container restarts so the reconciler never loses
a position's stop/target metadata and trade history is always queryable.

Architecture:
    Each trader container connects to the shared MySQL instance. Every position
    is INSERTed at open time with full metadata (stops, targets, setup name).
    On close, the row is updated with exit details. Completed trades are copied
    to the `trades` table for permanent history.

Source-of-truth for reconciliation:
    On startup, open positions are loaded from MySQL BEFORE querying Alpaca.
    If a position exists in MySQL but Alpaca doesn't know about it, it was
    already closed server-side (likely a stop/target hit while the container
    was restarting) — we mark it closed in MySQL and skip it. If Alpaca has a
    position MySQL doesn't know about, that's a true orphan and we adopt it.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy import (
    Column, Integer, String, Numeric, DateTime, Boolean, Enum, Index,
    ForeignKey, Date, create_engine, text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship
from urllib.parse import quote_plus as urlquote

from state.position_book import OpenPosition, PositionBook

log = logging.getLogger("mysql_store")

# ── ORM Models ──────────────────────────────────────────────────────────────

class Base(DeclarativeBase):
    pass


class StrategyRow(Base):
    __tablename__ = "strategies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    # created_at: TIMESTAMP DEFAULT CURRENT_TIMESTAMP

    positions = relationship("PositionRow", back_populates="strategy")


class PositionRow(Base):
    __tablename__ = "positions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    strategy_id: Mapped[int] = mapped_column(Integer, ForeignKey("strategies.id"), nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    asset_class: Mapped[str] = mapped_column(String(16), nullable=False)
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    qty: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    entry_px: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    stop_px: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 8), nullable=True)
    target_px: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 8), nullable=True)
    initial_stop_px: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 8), nullable=True)
    setup_name: Mapped[str] = mapped_column(String(64), nullable=False)
    order_id: Mapped[str] = mapped_column(String(64), default="")
    stop_order_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    breakeven_moved: Mapped[bool] = mapped_column(Boolean, default=False)
    bars_held: Mapped[int] = mapped_column(Integer, default=0)
    adopted: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(Enum("open", "closed"), default="open")
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    closed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    close_reason: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    exit_px: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 8), nullable=True)
    pnl_usd: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 8), nullable=True)
    R_realized: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 8), nullable=True)

    strategy = relationship("StrategyRow", back_populates="positions")

    __table_args__ = (
        Index("idx_open", "strategy_id", "status", "symbol"),
    )


class TradeRow(Base):
    __tablename__ = "trades"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    strategy_id: Mapped[int] = mapped_column(Integer, ForeignKey("strategies.id"), nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    asset_class: Mapped[str] = mapped_column(String(16), nullable=False)
    setup_name: Mapped[str] = mapped_column(String(64), nullable=False)
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    qty: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    entry_px: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    exit_px: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    stop_px: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 8), nullable=True)
    target_px: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 8), nullable=True)
    initial_stop_px: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 8), nullable=True)
    pnl_usd: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    R_realized: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    close_reason: Mapped[str] = mapped_column(String(32), nullable=False)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    closed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    bars_held: Mapped[int] = mapped_column(Integer, default=0)
    reflected: Mapped[bool] = mapped_column(Boolean, default=False)

    __table_args__ = (
        Index("idx_trades_time", "strategy_id", "closed_at"),
        Index("idx_trades_symbol", "strategy_id", "symbol"),
    )


# ── Store ───────────────────────────────────────────────────────────────────

def _build_url() -> str:
    """Build a MySQL connection URL from environment variables.

    MYSQL_HOST / MYSQL_PORT / MYSQL_USER / MYSQL_PASSWORD / MYSQL_DATABASE
    Defaults match the docker-compose mysql service definition.
    """
    host = os.environ.get("MYSQL_HOST", "mysql")
    port = os.environ.get("MYSQL_PORT", "3306")
    user = os.environ.get("MYSQL_USER", "trader")
    password = os.environ.get("MYSQL_PASSWORD", "traderpass")
    database = os.environ.get("MYSQL_DATABASE", "aitrader")
    return f"mysql+pymysql://{user}:{urlquote(password)}@{host}:{port}/{database}"


class MySQLStore:
    """Persist positions and trades to MySQL.

    Usage:
        store = MySQLStore(strategy_name="vwap_wave")
        store.ensure_schema()          # idempotent schema boot
        store.upsert_strategy()        # registers this strategy name
        store.position_opened(pos, asset_class="crypto")
        ...
        store.position_closed(symbol, exit_px=2100.0, close_reason="target")

    On startup, load all open positions for this strategy:
        book = store.load_open_positions()
    """

    def __init__(self, strategy_name: str, *, logger: logging.Logger | None = None):
        self.strategy_name = strategy_name
        self._log = logger or log
        self._strategy_id: int | None = None
        self._engine = create_engine(
            _build_url(),
            pool_pre_ping=True,
            pool_recycle=3600,
            connect_args={"connect_timeout": 5},
        )

    def ensure_schema(self) -> None:
        """Create tables if they don't exist. Idempotent. Also applies migrations."""
        Base.metadata.create_all(self._engine)
        # Migration: add reflected column if it doesn't exist (for existing DBs)
        try:
            with self._engine.connect() as conn:
                conn.execute(text(
                    "ALTER TABLE trades ADD COLUMN reflected TINYINT(1) DEFAULT 0"
                ))
                conn.commit()
        except Exception:
            pass  # Column already exists (or fresh DB from schema.sql)

    def upsert_strategy(self) -> int:
        """INSERT (or get) the strategy row, return its id."""
        with Session(self._engine) as session:
            row = session.query(StrategyRow).filter(
                StrategyRow.name == self.strategy_name
            ).one_or_none()
            if row is None:
                row = StrategyRow(name=self.strategy_name)
                session.add(row)
                session.commit()
                session.refresh(row)
            self._strategy_id = row.id
            return row.id

    @property
    def strategy_id(self) -> int:
        if self._strategy_id is None:
            self.upsert_strategy()
        assert self._strategy_id is not None
        return self._strategy_id

    # ── Position lifecycle ──────────────────────────────────────────────

    @staticmethod
    def _pos_to_dict(pos: OpenPosition, asset_class: str,
                     strategy_id: int) -> dict:
        return {
            "strategy_id": strategy_id,
            "symbol": pos.symbol,
            "asset_class": asset_class,
            "side": pos.side,
            "qty": Decimal(str(pos.qty)),
            "entry_px": Decimal(str(pos.entry_px)),
            "stop_px": Decimal(str(pos.stop_px)) if pos.stop_px is not None else None,
            "target_px": Decimal(str(pos.target_px)) if pos.target_px is not None else None,
            "initial_stop_px": Decimal(str(pos.initial_stop_px)) if pos.initial_stop_px is not None else None,
            "setup_name": pos.setup,
            "order_id": pos.order_id or "",
            "stop_order_id": pos.stop_order_id or None,
            "breakeven_moved": pos.breakeven_moved,
            "bars_held": pos.bars_held,
            "adopted": pos.adopted,
            "status": "open",
            "opened_at": pos.opened_at,
        }

    @staticmethod
    def _dict_to_pos(row: PositionRow) -> OpenPosition:
        return OpenPosition(
            symbol=row.symbol,
            setup=row.setup_name,
            side=row.side,
            qty=float(row.qty),
            entry_px=float(row.entry_px),
            stop_px=float(row.stop_px) if row.stop_px is not None else None,
            target_px=float(row.target_px) if row.target_px is not None else None,
            opened_at=row.opened_at,
            order_id=row.order_id or "",
            stop_order_id=row.stop_order_id,
            initial_stop_px=float(row.initial_stop_px) if row.initial_stop_px is not None else None,
            breakeven_moved=row.breakeven_moved,
            bars_held=row.bars_held,
            adopted=row.adopted,
        )

    def position_opened(self, pos: OpenPosition, asset_class: str) -> None:
        """Persist a newly opened position."""
        data = self._pos_to_dict(pos, asset_class, self.strategy_id)
        with Session(self._engine) as session:
            row = PositionRow(**data)
            session.add(row)
            session.commit()
            self._log.info(
                "MYSQL_POSITION_OPENED symbol=%s setup=%s side=%s qty=%s entry=%.4f",
                pos.symbol, pos.setup, pos.side, pos.qty, pos.entry_px,
            )

    def position_closed(
        self,
        symbol: str,
        exit_px: float,
        close_reason: str,
        closed_at: datetime | None = None,
    ) -> dict | None:
        """Close the open position for `symbol` and archive it to trades.

        Returns the closed position data dict, or None if no open position found.
        """
        if closed_at is None:
            closed_at = datetime.now(timezone.utc)

        with Session(self._engine) as session:
            row = session.query(PositionRow).filter(
                PositionRow.strategy_id == self.strategy_id,
                PositionRow.symbol == symbol,
                PositionRow.status == "open",
            ).one_or_none()

            if row is None:
                self._log.warning(
                    "MYSQL_CLOSE_NOT_FOUND symbol=%s strategy=%s",
                    symbol, self.strategy_name,
                )
                return None

            # Calculate PnL
            exit_dec = Decimal(str(exit_px))
            side_mult = Decimal("1") if row.side == "long" else Decimal("-1")
            pnl_usd = (exit_dec - row.entry_px) * side_mult * row.qty

            # R-realized
            stop_ref = row.initial_stop_px if row.initial_stop_px is not None else row.stop_px
            if stop_ref is not None:
                risk_per_share = abs(row.entry_px - stop_ref)
                R_realized = (exit_dec - row.entry_px) * side_mult / risk_per_share if risk_per_share > 0 else Decimal("0")
            else:
                R_realized = Decimal("0")

            # Update position row
            row.status = "closed"
            row.exit_px = exit_dec
            row.close_reason = close_reason
            row.closed_at = closed_at
            row.pnl_usd = pnl_usd
            row.R_realized = R_realized
            row.bars_held = row.bars_held  # keep last known

            # Archive to trades table
            trade = TradeRow(
                strategy_id=self.strategy_id,
                symbol=row.symbol,
                asset_class=row.asset_class,
                setup_name=row.setup_name,
                side=row.side,
                qty=row.qty,
                entry_px=row.entry_px,
                exit_px=exit_dec,
                stop_px=row.stop_px,
                target_px=row.target_px,
                initial_stop_px=row.initial_stop_px,
                pnl_usd=pnl_usd,
                R_realized=R_realized,
                close_reason=close_reason,
                opened_at=row.opened_at,
                closed_at=closed_at,
                bars_held=row.bars_held,
            )
            session.add(trade)
            session.commit()

            self._log.info(
                "MYSQL_POSITION_CLOSED symbol=%s reason=%s exit=%.4f pnl=%.4f R=%.2f",
                symbol, close_reason, exit_px, float(pnl_usd), float(R_realized),
            )
            return {
                "symbol": symbol,
                "pnl_usd": float(pnl_usd),
                "R_realized": float(R_realized),
                "close_reason": close_reason,
            }

    def load_open_positions(self) -> PositionBook:
        """Load all open positions for this strategy from MySQL.

        Returns a PositionBook suitable for feeding to the reconciler.
        """
        book = PositionBook()
        with Session(self._engine) as session:
            rows = session.query(PositionRow).filter(
                PositionRow.strategy_id == self.strategy_id,
                PositionRow.status == "open",
            ).all()
            for row in rows:
                pos = self._dict_to_pos(row)
                book.add(pos)
            self._log.info(
                "MYSQL_LOADED_OPEN_POSITIONS strategy=%s count=%d",
                self.strategy_name, book.count(),
            )
        return book

    def sync_position_state(self, pos: OpenPosition, asset_class: str) -> None:
        """Update mutable state on an existing open position (bars_held, breakeven, stop moves).

        Called each cycle to keep the DB in sync with in-memory book.
        """
        with Session(self._engine) as session:
            row = session.query(PositionRow).filter(
                PositionRow.strategy_id == self.strategy_id,
                PositionRow.symbol == pos.symbol,
                PositionRow.status == "open",
            ).one_or_none()
            if row is None:
                return
            if pos.stop_px != pos.initial_stop_px and not pos.breakeven_moved:
                row.breakeven_moved = True
                row.stop_px = Decimal(str(pos.stop_px)) if pos.stop_px is not None else None
            row.bars_held = pos.bars_held
            session.commit()

    def update_position_qty(self, symbol: str, new_qty: float) -> None:
        """Update the quantity of an existing open position.

        Called by the reconciler when drift is detected — the broker is the
        source of truth for position size (aggregated across strategies on the
        same account).
        """
        with Session(self._engine) as session:
            row = session.query(PositionRow).filter(
                PositionRow.strategy_id == self.strategy_id,
                PositionRow.symbol == symbol,
                PositionRow.status == "open",
            ).one_or_none()
            if row is None:
                self._log.warning(
                    "MYSQL_QTY_UPDATE_NOT_FOUND symbol=%s — no open position to update",
                    symbol,
                )
                return
            old_qty = float(row.qty)
            row.qty = Decimal(str(new_qty))
            session.commit()
            self._log.info(
                "MYSQL_QTY_UPDATED symbol=%s old_qty=%s new_qty=%s",
                symbol, old_qty, new_qty,
            )

    # ── Closed positions cleanup on startup ─────────────────────────────

    def close_positions_not_in_broker(self, broker_symbols: set[str]) -> list[str]:
        """Close any open MySQL positions whose symbols are NOT in broker_symbols.

        These are positions that were closed server-side (stop/target hit)
        while the container was restarting. We record them as closed with
        reason='reconciled_gone' since we don't have the exit price.

        Returns list of symbols that were closed.
        """
        closed = []
        with Session(self._engine) as session:
            rows = session.query(PositionRow).filter(
                PositionRow.strategy_id == self.strategy_id,
                PositionRow.status == "open",
            ).all()
            for row in rows:
                if row.symbol not in broker_symbols:
                    # Position was closed on broker side — mark it in DB
                    # We don't have exit_px, so we use entry_px as PnL=0 signal
                    # The reconciler will log this; user can manually adjust if needed
                    row.status = "closed"
                    row.close_reason = "reconciled_gone"
                    row.closed_at = datetime.now(timezone.utc)
                    row.exit_px = row.entry_px  # placeholder — unknown real exit
                    row.pnl_usd = Decimal("0")
                    row.R_realized = Decimal("0")

                    # Still archive for audit trail
                    trade = TradeRow(
                        strategy_id=self.strategy_id,
                        symbol=row.symbol,
                        asset_class=row.asset_class,
                        setup_name=row.setup_name,
                        side=row.side,
                        qty=row.qty,
                        entry_px=row.entry_px,
                        exit_px=row.entry_px,
                        stop_px=row.stop_px,
                        target_px=row.target_px,
                        initial_stop_px=row.initial_stop_px,
                        pnl_usd=Decimal("0"),
                        R_realized=Decimal("0"),
                        close_reason="reconciled_gone",
                        opened_at=row.opened_at,
                        closed_at=datetime.now(timezone.utc),
                        bars_held=row.bars_held,
                    )
                    session.add(trade)
                    closed.append(row.symbol)
                    self._log.warning(
                        "MYSQL_CLOSED_GONE symbol=%s — existed in DB but not on broker",
                        row.symbol,
                    )
            session.commit()
        return closed

    def get_recent_trades(self, limit: int = 50) -> list[dict]:
        """Retrieve the most recent completed trades for this strategy."""
        with Session(self._engine) as session:
            rows = (
                session.query(TradeRow)
                .filter(TradeRow.strategy_id == self.strategy_id)
                .order_by(TradeRow.closed_at.desc())
                .limit(limit)
                .all()
            )
            return [
                {
                    "symbol": r.symbol,
                    "setup": r.setup_name,
                    "side": r.side,
                    "qty": float(r.qty),
                    "entry_px": float(r.entry_px),
                    "exit_px": float(r.exit_px),
                    "pnl_usd": float(r.pnl_usd),
                    "R_realized": float(r.R_realized),
                    "close_reason": r.close_reason,
                    "opened_at": r.opened_at.isoformat(),
                    "closed_at": r.closed_at.isoformat() if r.closed_at else None,
                }
                for r in rows
            ]

    def strategy_stats(self, days: int = 30) -> dict:
        """Compute strategy stats over the last N days."""
        with Session(self._engine) as session:
            result = session.execute(text("""
                SELECT
                    COUNT(*) AS total_trades,
                    SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END) AS wins,
                    SUM(CASE WHEN pnl_usd < 0 THEN 1 ELSE 0 END) AS losses,
                    COALESCE(SUM(pnl_usd), 0) AS total_pnl,
                    COALESCE(AVG(R_realized), 0) AS avg_R
                FROM trades
                WHERE strategy_id = :sid
                  AND closed_at >= DATE_SUB(NOW(), INTERVAL :days DAY)
            """), {"sid": self.strategy_id, "days": days}).one()

        return {
            "total_trades": result.total_trades,
            "wins": result.wins or 0,
            "losses": result.losses or 0,
            "total_pnl": float(result.total_pnl or 0),
            "avg_R": float(result.avg_R or 0),
        }

    def get_unreflected_trades(self, limit: int = 100) -> list[dict]:
        """Return trades not yet processed by the optimizer reflection loop.

        Returns list of dicts with id, symbol, setup_name, entry_px, exit_px,
        pnl_usd, R_realized, close_reason, opened_at, closed_at.
        Ordered oldest-first so reflection is chronological.
        """
        with Session(self._engine) as session:
            rows = (
                session.query(TradeRow)
                .filter(
                    TradeRow.strategy_id == self.strategy_id,
                    TradeRow.reflected == False,
                )
                .order_by(TradeRow.closed_at.asc())
                .limit(limit)
                .all()
            )
            return [
                {
                    "id": r.id,
                    "symbol": r.symbol,
                    "setup_name": r.setup_name,
                    "side": r.side,
                    "qty": float(r.qty),
                    "entry_px": float(r.entry_px),
                    "exit_px": float(r.exit_px),
                    "stop_px": float(r.stop_px) if r.stop_px is not None else None,
                    "target_px": float(r.target_px) if r.target_px is not None else None,
                    "pnl_usd": float(r.pnl_usd),
                    "R_realized": float(r.R_realized),
                    "close_reason": r.close_reason,
                    "opened_at": r.opened_at.isoformat(),
                    "closed_at": r.closed_at.isoformat() if r.closed_at else None,
                }
                for r in rows
            ]

    def mark_trades_reflected(self, trade_ids: list[int]) -> int:
        """Mark trades as processed by the optimizer. Returns count updated."""
        if not trade_ids:
            return 0
        with Session(self._engine) as session:
            count = (
                session.query(TradeRow)
                .filter(TradeRow.id.in_(trade_ids))
                .update({"reflected": True}, synchronize_session=False)
            )
            session.commit()
            self._log.info(
                "MYSQL_MARKED_REFLECTED strategy=%s count=%d ids=%s",
                self.strategy_name, count, trade_ids,
            )
            return count

    def count_unreflected(self) -> int:
        """Count of trades not yet reflected."""
        with Session(self._engine) as session:
            return (
                session.query(TradeRow)
                .filter(
                    TradeRow.strategy_id == self.strategy_id,
                    TradeRow.reflected == False,
                )
                .count()
            )
