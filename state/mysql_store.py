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

import json
import logging
import os
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Callable, Optional

from sqlalchemy import (
    Column, Integer, String, Numeric, DateTime, Boolean, Enum, Index,
    ForeignKey, Date, JSON, create_engine, text,
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

    # Operator kill-switch. enabled is the boolean the trader polls each cycle;
    # state is the UI-facing tri-state ("enabled" | "disabling" | "disabled").
    # Both exist because the trader transitions disabling → disabled itself
    # once its book empties, while the dashboard updates state synchronously
    # when an operator clicks Disable.
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    state: Mapped[str] = mapped_column(
        Enum("enabled", "disabling", "disabled", name="strategy_state"),
        default="enabled", nullable=False,
    )
    last_change_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    last_change_reason: Mapped[Optional[str]] = mapped_column(
        String(255), nullable=True,
    )

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
    client_order_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    exit_client_order_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    legacy_untagged: Mapped[bool] = mapped_column(Boolean, default=False)
    breakeven_moved: Mapped[bool] = mapped_column(Boolean, default=False)
    bars_held: Mapped[int] = mapped_column(Integer, default=0)
    adopted: Mapped[bool] = mapped_column(Boolean, default=False)
    # See OpenPosition.fill_confirmed — once True the engine can act on
    # virtual stop/target checks; while False, on_bar defers and will
    # poll Alpaca once before re-checking.
    fill_confirmed: Mapped[bool] = mapped_column(Boolean, default=False)
    # See OpenPosition.exit_submitted — once True, PositionManager.on_bar
    # treats this position as exit-in-flight and stops emitting further
    # virtual exit actions. Cleared by the row leaving the table when the
    # reconciler closes it from the broker's actual close fill.
    exit_submitted: Mapped[bool] = mapped_column(Boolean, default=False)
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
        Index("idx_client_order_id", "client_order_id"),
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
    client_order_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    exit_client_order_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)

    __table_args__ = (
        Index("idx_trades_time", "strategy_id", "closed_at"),
        Index("idx_trades_symbol", "strategy_id", "symbol"),
        Index("idx_trades_client_order_id", "client_order_id"),
    )


class StrikeRow(Base):
    __tablename__ = "reconciliation_strikes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # 'key' collides with SQL keyword in some dialects; SQLAlchemy quotes it.
    key: Mapped[str] = mapped_column("key", String(128), nullable=False)
    direction: Mapped[str] = mapped_column(
        Enum("qty_drift", "mysql_only", "broker_only"), nullable=False
    )
    strategy_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("strategies.id"), nullable=True
    )
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    # Nullable so pre-migration rows can coexist; per-asset-class reconcilers
    # adopt NULLs on their own side (see reconciler/strikes.py).
    asset_class: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    strike_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_observed_state: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    resolved: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_reason: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    __table_args__ = (
        Index("idx_strikes_key", "key", "resolved"),
        Index("idx_strikes_unresolved", "resolved", "last_seen_at"),
        Index("idx_strikes_asset_class", "asset_class", "resolved", "last_seen_at"),
    )


class EventRow(Base):
    __tablename__ = "reconciliation_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    type: Mapped[str] = mapped_column(String(32), nullable=False)
    strategy_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("strategies.id"), nullable=True
    )
    symbol: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    # Nullable: events emitted by older reconciler builds carry NULL until
    # they roll out of the dashboard's lookback window.
    asset_class: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    payload: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        Index("idx_events_time", "created_at"),
        Index("idx_events_type", "type", "created_at"),
        Index("idx_events_asset_class", "asset_class", "type", "created_at"),
    )


class BrokerCredentialsRow(Base):
    __tablename__ = "broker_credentials"

    asset_class: Mapped[str] = mapped_column(String(16), primary_key=True)
    api_key: Mapped[str] = mapped_column(String(255), nullable=False)
    secret_key: Mapped[str] = mapped_column(String(255), nullable=False)
    base_url: Mapped[str] = mapped_column(String(255), nullable=False)
    account_number: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


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
        """Create tables if they don't exist. Idempotent. Also applies migrations.

        For existing DBs we cannot rely on create_all to add new columns, so each
        new column gets its own try/except ALTER. Order matters only for the
        legacy_untagged backfill (must run after the column exists).
        """
        Base.metadata.create_all(self._engine)

        migrations: list[str] = [
            # trades.reflected — historic, kept for backwards compat
            "ALTER TABLE trades ADD COLUMN reflected TINYINT(1) DEFAULT 0",
            # positions: client_order_id columns + legacy_untagged
            "ALTER TABLE positions ADD COLUMN client_order_id VARCHAR(128) DEFAULT NULL",
            "ALTER TABLE positions ADD COLUMN exit_client_order_id VARCHAR(128) DEFAULT NULL",
            "ALTER TABLE positions ADD COLUMN legacy_untagged TINYINT(1) DEFAULT 0",
            "CREATE INDEX idx_client_order_id ON positions (client_order_id)",
            # trades: client_order_id columns
            "ALTER TABLE trades ADD COLUMN client_order_id VARCHAR(128) DEFAULT NULL",
            "ALTER TABLE trades ADD COLUMN exit_client_order_id VARCHAR(128) DEFAULT NULL",
            "CREATE INDEX idx_trades_client_order_id ON trades (client_order_id)",
            # strategies: operator kill-switch + UI-facing tri-state.
            "ALTER TABLE strategies ADD COLUMN enabled TINYINT(1) NOT NULL DEFAULT 1",
            "ALTER TABLE strategies ADD COLUMN state ENUM('enabled','disabling','disabled') "
            "NOT NULL DEFAULT 'enabled'",
            "ALTER TABLE strategies ADD COLUMN last_change_at TIMESTAMP NULL",
            "ALTER TABLE strategies ADD COLUMN last_change_reason VARCHAR(255) NULL",
            # reconciliation tables: per-asset-class scoping (see plan
            # hashed-squishing-rocket). Nullable to keep legacy rows valid;
            # they get re-stamped on the next reconciler cycle that adopts them.
            "ALTER TABLE reconciliation_strikes ADD COLUMN asset_class VARCHAR(16) DEFAULT NULL",
            "CREATE INDEX idx_strikes_asset_class "
            "ON reconciliation_strikes (asset_class, resolved, last_seen_at)",
            "ALTER TABLE reconciliation_events ADD COLUMN asset_class VARCHAR(16) DEFAULT NULL",
            "CREATE INDEX idx_events_asset_class "
            "ON reconciliation_events (asset_class, type, created_at)",
            # positions.fill_confirmed: gates engine virtual exits until the
            # broker actually filled the entry. Default 1 for existing rows
            # so the migration doesn't accidentally freeze in-flight
            # positions across the upgrade boundary; new inserts default 0
            # via the SQLAlchemy column default.
            "ALTER TABLE positions ADD COLUMN fill_confirmed TINYINT(1) "
            "NOT NULL DEFAULT 1",
            # positions.exit_submitted: gates engine virtual exits AFTER a
            # close has been submitted (the symmetric counterpart to
            # fill_confirmed gating BEFORE the entry is confirmed). Default
            # 0 for existing rows — they get one re-evaluation cycle, which
            # is fine because the close COID is now setup-tagged so the
            # reconciler will match the resulting close fill.
            "ALTER TABLE positions ADD COLUMN exit_submitted TINYINT(1) "
            "NOT NULL DEFAULT 0",
        ]
        try:
            with self._engine.connect() as conn:
                for stmt in migrations:
                    try:
                        conn.execute(text(stmt))
                        conn.commit()
                    except Exception as exc:
                        msg = str(exc).lower()
                        if "duplicate column" in msg or "duplicate key" in msg:
                            # Expected on already-applied / fresh DB; swallow.
                            continue
                        self._log.warning(
                            "MYSQL_MIGRATION_UNEXPECTED stmt=%r err=%s",
                            stmt, exc,
                        )
        except Exception as exc:
            # Could not even open a connection — surface this loudly.
            self._log.error(
                "MYSQL_MIGRATION_CONNECT_FAILED: %s", exc, exc_info=True,
            )

        # One-shot backfill: any row currently open with no client_order_id
        # is a pre-migration legacy position. Mark it so the reconciler service
        # in Plan 3 treats it as alert-only and never auto-mutates it.
        try:
            with self._engine.connect() as conn:
                conn.execute(text(
                    "UPDATE positions "
                    "SET legacy_untagged = 1 "
                    "WHERE status = 'open' "
                    "AND client_order_id IS NULL "
                    "AND legacy_untagged = 0"
                ))
                conn.commit()
        except Exception as exc:
            self._log.warning("MYSQL_LEGACY_BACKFILL_FAILED: %s", exc)

        # One-shot duplicate-symbol-form cleanup. Two open rows that share
        # (strategy_id, setup_name) but differ only in slash form (DOGE/USD
        # vs DOGEUSD) are a write-time race artifact between the engine's
        # optimistic insert and the reconciler's entry-recovery insert.
        # The newer row is most likely the broker-confirmed one (the
        # reconciler only inserts on observed fills); close the older one
        # at exit_px=entry_px so the audit trail remains and the strategy
        # can boot. Idempotent — re-runs find nothing.
        self._consolidate_duplicate_open_positions()

    def _consolidate_duplicate_open_positions(self) -> None:
        try:
            with Session(self._engine) as session:
                # `pairs` rows: (older_id, newer_id, symbol, setup_name, strategy_id)
                pairs = session.execute(text(
                    "SELECT a.id AS older_id, b.id AS newer_id, "
                    "       a.symbol AS older_symbol, b.symbol AS newer_symbol, "
                    "       a.setup_name, a.strategy_id "
                    "FROM positions a "
                    "JOIN positions b "
                    "  ON a.strategy_id = b.strategy_id "
                    "  AND a.setup_name = b.setup_name "
                    "  AND REPLACE(a.symbol, '/', '') = REPLACE(b.symbol, '/', '') "
                    "  AND a.id < b.id "
                    "WHERE a.status = 'open' AND b.status = 'open'"
                )).all()
                if not pairs:
                    return
                now = datetime.now(timezone.utc)
                closed = 0
                for older_id, newer_id, older_sym, newer_sym, setup, sid in pairs:
                    older = session.get(PositionRow, older_id)
                    if older is None or older.status != "open":
                        continue
                    older.status = "closed"
                    older.exit_px = older.entry_px
                    older.close_reason = "duplicate_consolidation"
                    older.closed_at = now
                    older.pnl_usd = Decimal("0")
                    older.R_realized = Decimal("0")
                    session.add(TradeRow(
                        strategy_id=older.strategy_id,
                        symbol=older.symbol,
                        asset_class=older.asset_class,
                        setup_name=older.setup_name,
                        side=older.side,
                        qty=older.qty,
                        entry_px=older.entry_px,
                        exit_px=older.entry_px,
                        stop_px=older.stop_px,
                        target_px=older.target_px,
                        initial_stop_px=older.initial_stop_px,
                        client_order_id=older.client_order_id,
                        exit_client_order_id=None,
                        pnl_usd=Decimal("0"),
                        R_realized=Decimal("0"),
                        close_reason="duplicate_consolidation",
                        opened_at=older.opened_at,
                        closed_at=now,
                        bars_held=older.bars_held,
                    ))
                    closed += 1
                    self._log.warning(
                        "MYSQL_DUP_CLEANUP closed_id=%s kept_id=%s "
                        "older_symbol=%s newer_symbol=%s setup=%s strategy_id=%s",
                        older_id, newer_id, older_sym, newer_sym, setup, sid,
                    )
                session.commit()
                self._log.warning("MYSQL_DUP_CLEANUP closed=%d", closed)
        except Exception as exc:
            self._log.warning("MYSQL_DUP_CLEANUP_FAILED: %s", exc)

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

    # ── Operator kill-switch ────────────────────────────────────────────

    def is_strategy_enabled(self) -> bool:
        """Return the `enabled` flag for self.strategy_id. True on missing row.

        The trader main loop polls this each cycle. Default-true on lookup
        failure prevents a transient DB hiccup from silently halting trading.
        """
        try:
            with Session(self._engine) as session:
                row = session.query(StrategyRow).filter(
                    StrategyRow.id == self.strategy_id,
                ).one_or_none()
                return bool(row.enabled) if row is not None else True
        except Exception as exc:
            self._log.error("MYSQL_IS_ENABLED_QUERY_FAILED: %s", exc, exc_info=True)
            return True

    def get_strategy_state(self, strategy_id: int) -> str | None:
        """Return the UI tri-state ("enabled"/"disabling"/"disabled") or None."""
        with Session(self._engine) as session:
            row = session.query(StrategyRow).filter(
                StrategyRow.id == strategy_id,
            ).one_or_none()
            return row.state if row is not None else None

    def set_strategy_state(
        self, strategy_id: int, *, enabled: bool, state: str, reason: str,
    ) -> None:
        """Atomically update enabled/state/last_change_* + write an audit event.

        `state` must be one of "enabled" | "disabling" | "disabled". The
        EventRow lets the dashboard reconstruct the operator's intent and
        any subsequent automated transitions (the trader's self-heal ends a
        sweep with reason="trader_disable_sweep_complete").
        """
        if state not in {"enabled", "disabling", "disabled"}:
            raise ValueError(f"invalid strategy state: {state!r}")
        now = datetime.now(timezone.utc)
        with Session(self._engine) as session:
            row = session.query(StrategyRow).filter(
                StrategyRow.id == strategy_id,
            ).one()
            prev_state = row.state
            prev_enabled = bool(row.enabled)
            row.enabled = enabled
            row.state = state
            row.last_change_at = now
            row.last_change_reason = (reason or "")[:255]
            session.add(EventRow(
                type="strategy_state_changed",
                strategy_id=strategy_id,
                payload={
                    "from_state": prev_state, "to_state": state,
                    "from_enabled": prev_enabled, "to_enabled": enabled,
                    "reason": reason,
                },
                created_at=now,
            ))
            session.commit()

    def get_strategies_admin_view(self) -> list[dict]:
        """Per-strategy snapshot for the dashboard admin table.

        Aggregates: open count from positions, today/total PnL and win rate
        from trades. One row per known strategy (returns even strategies
        with no positions or trades, so an operator always sees the kill-
        switch row).
        """
        # SQLite (used by tests) doesn't support TIMESTAMPDIFF/CURDATE the
        # same way MySQL does; we compute "today" in Python and pass it in.
        from datetime import time as _time
        today_start = datetime.combine(
            datetime.now(timezone.utc).date(), _time.min, tzinfo=timezone.utc,
        )
        with Session(self._engine) as session:
            strategies = session.query(StrategyRow).order_by(StrategyRow.name).all()
            out: list[dict] = []
            for s in strategies:
                open_count = session.query(PositionRow).filter(
                    PositionRow.strategy_id == s.id,
                    PositionRow.status == "open",
                ).count()
                today_pnl = session.query(TradeRow).filter(
                    TradeRow.strategy_id == s.id,
                    TradeRow.closed_at >= today_start,
                ).all()
                total = session.query(TradeRow).filter(
                    TradeRow.strategy_id == s.id,
                ).all()
                today_pnl_sum = float(sum(float(t.pnl_usd) for t in today_pnl))
                total_pnl_sum = float(sum(float(t.pnl_usd) for t in total))
                wins = sum(1 for t in total if float(t.pnl_usd) > 0)
                win_rate = (wins / len(total)) if total else 0.0
                out.append({
                    "id": s.id,
                    "name": s.name,
                    "state": s.state,
                    "enabled": bool(s.enabled),
                    "open_count": open_count,
                    "today_pnl": today_pnl_sum,
                    "total_pnl": total_pnl_sum,
                    "win_rate": win_rate,
                    "trade_count": len(total),
                    "last_change_at": s.last_change_at,
                    "last_change_reason": s.last_change_reason,
                })
            return out

    # ── Position lifecycle ──────────────────────────────────────────────

    @staticmethod
    def _normalize_symbol(symbol: str) -> str:
        """Persist the broker-flat form so DOGE/USD and DOGEUSD never
        coexist as two open rows for the same (strategy, setup). Reads
        stay slash-aware via _get_symbol_candidates so legacy rows still
        match — only writes change."""
        return symbol.replace("/", "")

    @staticmethod
    def _pos_to_dict(pos: OpenPosition, asset_class: str,
                     strategy_id: int) -> dict:
        return {
            "strategy_id": strategy_id,
            "symbol": MySQLStore._normalize_symbol(pos.symbol),
            "asset_class": asset_class,
            "side": pos.side,
            "qty": Decimal(str(pos.qty)),
            "entry_px": Decimal(str(pos.entry_px)),
            "stop_px": Decimal(str(pos.stop_px)) if pos.stop_px is not None else None,
            "target_px": Decimal(str(pos.target_px)) if pos.target_px is not None else None,
            "initial_stop_px": Decimal(str(pos.initial_stop_px)) if pos.initial_stop_px is not None else None,
            "setup_name": pos.setup,
            "order_id": pos.order_id or "",
            "client_order_id": pos.client_order_id,
            "stop_order_id": pos.stop_order_id or None,
            "breakeven_moved": pos.breakeven_moved,
            "bars_held": pos.bars_held,
            "adopted": pos.adopted,
            "fill_confirmed": pos.fill_confirmed,
            "exit_submitted": pos.exit_submitted,
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
            client_order_id=row.client_order_id,
            stop_order_id=row.stop_order_id,
            initial_stop_px=float(row.initial_stop_px) if row.initial_stop_px is not None else None,
            breakeven_moved=row.breakeven_moved,
            bars_held=row.bars_held,
            adopted=row.adopted,
            fill_confirmed=row.fill_confirmed,
            exit_submitted=row.exit_submitted,
        )

    def mark_fill_confirmed(
        self, strategy_id: int, symbol: str, setup_name: str,
    ) -> bool:
        """Flip an open position's fill_confirmed flag to True.

        Called by PositionManager once it has independently verified the
        broker filled the entry order, so a restart doesn't re-poll.
        Returns True if the row was found and updated.
        """
        with Session(self._engine) as session:
            row = session.query(PositionRow).filter(
                PositionRow.strategy_id == strategy_id,
                PositionRow.symbol.in_(self._get_symbol_candidates(symbol)),
                PositionRow.setup_name == setup_name,
                PositionRow.status == "open",
            ).one_or_none()
            if row is None:
                return False
            row.fill_confirmed = True
            session.commit()
            return True

    def mark_exit_submitted(
        self, strategy_id: int, symbol: str, setup_name: str,
    ) -> bool:
        """Flip an open position's exit_submitted flag to True.

        Called by OrderExecutor.handle_actions immediately after submitting
        (or registering an in-flight bracket OCO firing for) a broker close,
        so PositionManager stops emitting further exit actions on the next
        bar. Returns True if the row was found and updated.

        Idempotent: re-applying on an already-True row is a no-op success.
        """
        with Session(self._engine) as session:
            row = session.query(PositionRow).filter(
                PositionRow.strategy_id == strategy_id,
                PositionRow.symbol.in_(self._get_symbol_candidates(symbol)),
                PositionRow.setup_name == setup_name,
                PositionRow.status == "open",
            ).one_or_none()
            if row is None:
                return False
            row.exit_submitted = True
            session.commit()
            return True

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

    def _get_symbol_candidates(self, symbol: str) -> list[str]:
        candidates = [symbol]
        if "/" in symbol:
            candidates.append(symbol.replace("/", ""))
        else:
            for quote in ("USD", "USDT", "USDC", "EUR", "GBP", "BTC", "ETH"):
                if symbol.endswith(quote) and len(symbol) > len(quote):
                    base = symbol[: -len(quote)]
                    if base.isalpha() and 2 <= len(base) <= 5:
                        candidates.append(f"{base}/{quote}")
                        break
        return candidates

    def position_closed(
        self,
        symbol: str,
        exit_px: float,
        close_reason: str,
        closed_at: datetime | None = None,
        setup_name: str | None = None,
        exit_client_order_id: str | None = None,
        strategy_id: int | None = None,
    ) -> dict | None:
        """Close the open position for `symbol` (and optionally setup) and archive it to trades.

        When setup_name is provided, closes the specific setup's position on
        that symbol. Without setup_name, closes any open position (legacy compat
        for single-setup-at-a-time usage).

        Returns the closed position data dict, or None if no open position found.
        """
        if closed_at is None:
            closed_at = datetime.now(timezone.utc)

        with Session(self._engine) as session:
            target_strategy_id = strategy_id if strategy_id is not None else self.strategy_id
            q = session.query(PositionRow).filter(
                PositionRow.strategy_id == target_strategy_id,
                PositionRow.symbol.in_(self._get_symbol_candidates(symbol)),
                PositionRow.status == "open",
            )
            if setup_name:
                q = q.filter(PositionRow.setup_name == setup_name)
            # all() instead of one_or_none() so a historical duplicate
            # (DOGEUSD + DOGE/USD with the same setup) is consolidated
            # at close time instead of raising MultipleResultsFound.
            rows = q.all()

            if not rows:
                self._log.warning(
                    "MYSQL_CLOSE_NOT_FOUND symbol=%s strategy=%s strategy_id=%d",
                    symbol, self.strategy_name, target_strategy_id,
                )
                return None

            if len(rows) > 1:
                self._log.warning(
                    "MYSQL_CLOSE_CONSOLIDATING_DUPLICATES symbol=%s "
                    "setup=%s count=%d row_ids=%s",
                    symbol, setup_name, len(rows), [r.id for r in rows],
                )

            # Calculate PnL once on the first row (used as the return value).
            # Each row archives its own TradeRow with its own qty/entry_px so
            # the audit trail stays per-row.
            exit_dec = Decimal(str(exit_px))
            primary = rows[0]
            primary_side_mult = Decimal("1") if primary.side == "long" else Decimal("-1")
            primary_pnl = (exit_dec - primary.entry_px) * primary_side_mult * primary.qty
            primary_stop_ref = (primary.initial_stop_px
                                if primary.initial_stop_px is not None
                                else primary.stop_px)
            if primary_stop_ref is not None:
                rps = abs(primary.entry_px - primary_stop_ref)
                primary_R = ((exit_dec - primary.entry_px) * primary_side_mult / rps
                             if rps > 0 else Decimal("0"))
            else:
                primary_R = Decimal("0")

            for row in rows:
                side_mult = Decimal("1") if row.side == "long" else Decimal("-1")
                pnl_usd = (exit_dec - row.entry_px) * side_mult * row.qty
                stop_ref = row.initial_stop_px if row.initial_stop_px is not None else row.stop_px
                if stop_ref is not None:
                    rps = abs(row.entry_px - stop_ref)
                    R_realized = ((exit_dec - row.entry_px) * side_mult / rps
                                  if rps > 0 else Decimal("0"))
                else:
                    R_realized = Decimal("0")

                # Update position row
                row.status = "closed"
                row.exit_px = exit_dec
                row.close_reason = close_reason
                row.closed_at = closed_at
                row.pnl_usd = pnl_usd
                row.R_realized = R_realized
                row.exit_client_order_id = exit_client_order_id

                # Archive to trades table
                trade = TradeRow(
                    strategy_id=target_strategy_id,
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
                    client_order_id=row.client_order_id,
                    exit_client_order_id=exit_client_order_id,
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
                "MYSQL_POSITION_CLOSED symbol=%s reason=%s exit=%.4f "
                "pnl=%.4f R=%.2f rows=%d",
                symbol, close_reason, exit_px,
                float(primary_pnl), float(primary_R), len(rows),
            )
            return {
                "symbol": symbol,
                "pnl_usd": float(primary_pnl),
                "R_realized": float(primary_R),
                "close_reason": close_reason,
            }

    def load_open_positions(self) -> PositionBook:
        """Load all open positions for this strategy from MySQL.

        Returns a PositionBook suitable for feeding to the reconciler.

        Resilient against historical duplicates: PositionBook.add raises
        on (normalized_symbol, setup) collisions, but a malformed DB
        (e.g. one row stored as DOGE/USD and another as DOGEUSD for the
        same setup) used to crash the strategy at startup. The cleanup
        in ensure_schema flattens these on boot, but the defensive
        try/except here means a freshly-introduced duplicate during a
        running cycle can't take the container down — the next close
        consolidates them.
        """
        book = PositionBook()
        skipped: list[int] = []
        with Session(self._engine) as session:
            rows = session.query(PositionRow).filter(
                PositionRow.strategy_id == self.strategy_id,
                PositionRow.status == "open",
            ).all()
            for row in rows:
                pos = self._dict_to_pos(row)
                try:
                    book.add(pos)
                except ValueError:
                    skipped.append(row.id)
                    self._log.error(
                        "MYSQL_DUPLICATE_OPEN_ROW strategy=%s symbol=%s "
                        "setup=%s row_id=%s — keeping the first seen row, "
                        "the duplicate will collapse on the next close",
                        self.strategy_name, row.symbol, row.setup_name,
                        row.id,
                    )
            self._log.info(
                "MYSQL_LOADED_OPEN_POSITIONS strategy=%s count=%d skipped_duplicates=%d",
                self.strategy_name, book.count(), len(skipped),
            )
        return book

    def merge_open_positions(self, book: PositionBook) -> list[str]:
        """Pull any open positions from MySQL not already in the in-memory book.

        Checks by (symbol, setup_name) so that multiple setups can have
        independent positions on the same symbol.

        Returns list of symbols that were newly added. This ensures strategies
        pick up positions created by other processes (reconciler, other traders,
        manual inserts) without requiring a full restart.
        """
        added: list[str] = []
        with Session(self._engine) as session:
            rows = session.query(PositionRow).filter(
                PositionRow.strategy_id == self.strategy_id,
                PositionRow.status == "open",
            ).all()
            for row in rows:
                # Check by (symbol, setup_name) — allow different setups
                # on the same symbol to coexist in the book.
                existing = book.get(row.symbol, row.setup_name)
                if existing is not None:
                    continue
                # Also check crypto alt format
                alt = row.symbol.replace("/", "").replace("USD", "/USD")
                if alt != row.symbol:
                    existing = book.get(alt, row.setup_name)
                    if existing is not None:
                        continue
                pos = self._dict_to_pos(row)
                try:
                    book.add(pos)
                    added.append(row.symbol)
                except ValueError:
                    pass  # already in book (race)
        if added:
            self._log.info(
                "MYSQL_MERGED_POSITIONS strategy=%s added=%s",
                self.strategy_name, added,
            )
        return added

    def sync_position_state(self, pos: OpenPosition, asset_class: str) -> None:
        """Update mutable state on an existing open position (bars_held, breakeven, stop moves).

        Called each cycle to keep the DB in sync with in-memory book.
        """
        with Session(self._engine) as session:
            q = session.query(PositionRow).filter(
                PositionRow.strategy_id == self.strategy_id,
                PositionRow.symbol.in_(self._get_symbol_candidates(pos.symbol)),
                PositionRow.setup_name == pos.setup,
                PositionRow.status == "open",
            )
            row = q.one_or_none()
            if row is None:
                self._log.warning(
                    "MYSQL_SYNC_NOT_FOUND symbol=%s setup=%s — skipping",
                    pos.symbol, pos.setup,
                )
                return
            if pos.stop_px != pos.initial_stop_px and not pos.breakeven_moved:
                row.breakeven_moved = True
                row.stop_px = Decimal(str(pos.stop_px)) if pos.stop_px is not None else None
            row.bars_held = pos.bars_held
            session.commit()

    def sum_qty_across_strategies(self, symbol: str) -> float:
        """Sum `qty` across ALL strategies for any open position on `symbol`.

        Used by the reconciler to decide whether broker-vs-local drift is
        explained by another strategy on the same account, or is a real
        accounting gap. Crypto symbols are matched in both flat and slash
        forms (BTCUSD ↔ BTC/USD).
        """
        alt = symbol.replace("/", "") if "/" in symbol else None
        with Session(self._engine) as session:
            q = session.query(PositionRow).filter(PositionRow.status == "open")
            if alt is not None:
                rows = q.filter(PositionRow.symbol.in_([symbol, alt])).all()
            else:
                # broker form is flat; book may also have stored slash form
                slash_alt = None
                for quote in ("USD", "USDT", "USDC", "EUR", "GBP", "BTC", "ETH"):
                    if symbol.endswith(quote) and len(symbol) > len(quote):
                        base = symbol[: -len(quote)]
                        if base.isalpha() and 2 <= len(base) <= 5:
                            slash_alt = f"{base}/{quote}"
                            break
                if slash_alt is not None:
                    rows = q.filter(PositionRow.symbol.in_([symbol, slash_alt])).all()
                else:
                    rows = q.filter(PositionRow.symbol == symbol).all()
            return float(sum((r.qty for r in rows), Decimal("0")))

    def count_strategies_holding(self, symbol: str) -> int:
        """Number of distinct strategies with an open position on `symbol`.

        The reconciler treats drift as auto-correctable only when this is 1
        (this strategy is the sole owner). Otherwise the broker total is the
        sum of multiple strategies and we cannot attribute drift unambiguously.
        """
        alt = symbol.replace("/", "") if "/" in symbol else None
        with Session(self._engine) as session:
            q = session.query(PositionRow.strategy_id).filter(
                PositionRow.status == "open"
            )
            if alt is not None:
                q = q.filter(PositionRow.symbol.in_([symbol, alt]))
            else:
                slash_alt = None
                for quote in ("USD", "USDT", "USDC", "EUR", "GBP", "BTC", "ETH"):
                    if symbol.endswith(quote) and len(symbol) > len(quote):
                        base = symbol[: -len(quote)]
                        if base.isalpha() and 2 <= len(base) <= 5:
                            slash_alt = f"{base}/{quote}"
                            break
                if slash_alt is not None:
                    q = q.filter(PositionRow.symbol.in_([symbol, slash_alt]))
                else:
                    q = q.filter(PositionRow.symbol == symbol)
            return len({sid for (sid,) in q.distinct()})

    def update_position_qty(self, symbol: str, new_qty: float) -> None:
        """Update the quantity of an existing open position.

        Called by the reconciler when drift is detected — the broker is the
        source of truth for position size (aggregated across strategies on the
        same account).
        """
        with Session(self._engine) as session:
            row = session.query(PositionRow).filter(
                PositionRow.strategy_id == self.strategy_id,
                PositionRow.symbol.in_(self._get_symbol_candidates(symbol)),
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

    # ── One-time JSON → MySQL migration ─────────────────────────────────

    def migrate_legacy_json(
        self,
        json_path: Path | str,
        asset_class_for: Callable[[str], str | None],
    ) -> int:
        """Import a legacy runtime/position_book_*.json into MySQL once.

        No-op when:
        - this strategy already has open positions in MySQL, OR
        - `json_path` does not exist.

        On success, renames the file to `<name>.json.migrated` so it is not
        re-imported. Returns the number of rows imported.
        """
        path = Path(json_path)
        if not path.exists():
            return 0
        if self.load_open_positions().count() > 0:
            return 0

        try:
            data = json.loads(path.read_text())
        except Exception as exc:
            self._log.error("MYSQL_LEGACY_MIGRATION_READ_FAILED path=%s: %s",
                            path, exc)
            return 0

        positions = data.get("positions") or []
        imported = 0
        for entry in positions:
            symbol = entry["symbol"]
            ac = asset_class_for(symbol)
            if ac is None:
                self._log.warning(
                    "MYSQL_LEGACY_MIGRATION_SKIP symbol=%s — no asset_class mapping",
                    symbol,
                )
                continue
            pos = OpenPosition(
                symbol=symbol,
                setup=entry["setup"],
                side=entry["side"],
                qty=float(entry["qty"]),
                entry_px=float(entry["entry_px"]),
                stop_px=None if entry.get("stop_px") is None else float(entry["stop_px"]),
                target_px=None if entry.get("target_px") is None else float(entry["target_px"]),
                opened_at=datetime.fromisoformat(entry["opened_at"]),
                order_id=entry.get("order_id", ""),
                breakeven_moved=bool(entry.get("breakeven_moved", False)),
                bars_held=int(entry.get("bars_held", 0)),
                stop_order_id=entry.get("stop_order_id"),
                initial_stop_px=(None if entry.get("initial_stop_px") is None
                                 else float(entry["initial_stop_px"])),
                adopted=bool(entry.get("adopted", False)),
                # Legacy state-file rows pre-date the fill_confirmed flag —
                # treat them as confirmed so the engine doesn't freeze on
                # them after the migration (mirrors the schema default).
                fill_confirmed=True,
                exit_submitted=False,
            )
            try:
                self.position_opened(pos, ac)
                imported += 1
            except Exception as exc:
                self._log.error(
                    "MYSQL_LEGACY_MIGRATION_INSERT_FAILED symbol=%s: %s",
                    symbol, exc,
                )

        if imported > 0 or positions:
            archive = path.with_suffix(path.suffix + ".migrated")
            os.replace(path, archive)
            self._log.info(
                "MYSQL_LEGACY_MIGRATED rows=%d source=%s archived=%s",
                imported, path, archive,
            )
        return imported

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

    def find_open_position_by_coid(self, client_order_id: str) -> "PositionRow | None":
        """Return the open PositionRow with this entry COID, or None.

        Used by the reconciler service to match Alpaca fills to MySQL rows.
        Crosses strategies — does not filter by self.strategy_id.

        NOTE: returned object is detached from its session — access scalar
        columns (symbol, strategy_id, etc.) safely; do NOT access lazy
        relationships like `.strategy` (raises DetachedInstanceError).
        """
        with Session(self._engine) as session:
            return session.query(PositionRow).filter(
                PositionRow.client_order_id == client_order_id,
                PositionRow.status == "open",
            ).one_or_none()

    def find_open_position_by_setup(
        self, strategy_id: int, symbol: str, setup_name: str,
    ) -> "PositionRow | None":
        """Return the open PositionRow for (strategy_id, symbol, setup_name).

        Crypto-symbol-form-aware (matches BTC/USD and BTCUSD).

        NOTE: returned object is detached from its session — access scalar
        columns (symbol, strategy_id, etc.) safely; do NOT access lazy
        relationships like `.strategy` (raises DetachedInstanceError).
        """
        candidates = self._get_symbol_candidates(symbol)
        with Session(self._engine) as session:
            return session.query(PositionRow).filter(
                PositionRow.strategy_id == strategy_id,
                PositionRow.symbol.in_(candidates),
                PositionRow.setup_name == setup_name,
                PositionRow.status == "open",
            ).one_or_none()

    def insert_position_from_fill(
        self,
        strategy_id: int,
        setup_name: str,
        symbol: str,
        side: str,
        qty: float,
        entry_px: float,
        opened_at: datetime,
        asset_class: str,
        client_order_id: str,
    ) -> int:
        """Insert a position row recovered from a tagged Alpaca entry fill.

        Used when a strategy submitted an order, Alpaca filled it, but the
        strategy crashed before writing position_opened() to MySQL. The COID
        proves the strategy intended to open this position. Returns the new
        row id. adopted=False because this is recovery, not adoption.
        """
        with Session(self._engine) as session:
            row = PositionRow(
                strategy_id=strategy_id,
                symbol=self._normalize_symbol(symbol),
                asset_class=asset_class,
                side=side,
                qty=Decimal(str(qty)),
                entry_px=Decimal(str(entry_px)),
                stop_px=None,
                target_px=None,
                initial_stop_px=None,
                setup_name=setup_name,
                order_id="",
                client_order_id=client_order_id,
                stop_order_id=None,
                breakeven_moved=False,
                bars_held=0,
                adopted=False,
                # The fill is already on the broker — that's why we're
                # inserting this row. Stamp confirmed so the engine acts
                # immediately on virtual exit checks.
                fill_confirmed=True,
                exit_submitted=False,
                status="open",
                opened_at=opened_at,
            )
            session.add(row)
            session.commit()
            session.refresh(row)
            self._log.info(
                "MYSQL_FILL_RECOVERED strategy_id=%d symbol=%s setup=%s qty=%s coid=%s",
                strategy_id, symbol, setup_name, qty, client_order_id,
            )
            return row.id

    def sum_qty_by_symbol(
        self, asset_class: str | None = None,
    ) -> dict[str, float]:
        """Aggregate open SIGNED qty per symbol across ALL strategies.

        Returns positive qty for net-long, negative for net-short. Brokers
        like Alpaca already return signed qty, so the reconciler invariant
        compares like-with-like — long-vs-short divergence between MySQL and
        broker is caught instead of silently treated as equal magnitudes.

        Crypto symbols are normalized to broker-flat form (BTC/USD → BTCUSD)
        so multi-format storage doesn't double-count. Accumulates as Decimal
        for precision parity with sum_qty_across_strategies, converting to
        float only at the boundary.

        ``asset_class`` filters the aggregation to that side. Default None
        keeps the legacy global view (used by ad-hoc tooling).
        """
        out: dict[str, Decimal] = {}
        with Session(self._engine) as session:
            q = session.query(
                PositionRow.symbol, PositionRow.side, PositionRow.qty,
            ).filter(PositionRow.status == "open")
            if asset_class is not None:
                q = q.filter(PositionRow.asset_class == asset_class)
            for symbol, side, qty in q.all():
                # Normalize: any "X/Y" form collapses to "XY".
                key = symbol.replace("/", "")
                signed_qty = qty if side == "long" else -qty
                out[key] = out.get(key, Decimal("0")) + signed_qty
        return {k: float(v) for k, v in out.items()}

    def list_unresolved_strikes(self) -> list["StrikeRow"]:
        """Return all unresolved reconciliation strikes, newest last_seen first."""
        with Session(self._engine) as session:
            rows = session.query(StrikeRow).filter(
                StrikeRow.resolved == False,  # noqa: E712
            ).order_by(StrikeRow.last_seen_at.desc()).all()
            for r in rows:
                session.expunge(r)
            return rows

    def get_strike_by_id(self, strike_id: int) -> "StrikeRow | None":
        """Return the StrikeRow with this id, or None.

        NOTE: returned object is detached — access scalar columns only.
        """
        with Session(self._engine) as session:
            row = session.query(StrikeRow).filter(
                StrikeRow.id == strike_id,
            ).one_or_none()
            if row is not None:
                session.expunge(row)
            return row

    def resolve_strike(
        self, strike_id: int, *, reason: str, operator_note: str,
    ) -> bool:
        """Flip a strike to resolved with operator-supplied reason.

        Writes an `operator_action` event for audit trail. Returns False
        if the strike doesn't exist or is already resolved.
        """
        with Session(self._engine) as session:
            row = session.query(StrikeRow).filter(
                StrikeRow.id == strike_id,
            ).one_or_none()
            if row is None or row.resolved:
                return False
            row.resolved = True
            row.resolved_at = datetime.now(timezone.utc)
            row.resolved_reason = reason
            session.add(EventRow(
                type="operator_action",
                strategy_id=row.strategy_id,
                symbol=row.symbol,
                payload={
                    "strike_id": strike_id,
                    "key": row.key,
                    "direction": row.direction,
                    "resolved_reason": reason,
                    "operator_note": operator_note,
                },
            ))
            session.commit()
            return True

    def recent_events(self, limit: int = 50) -> list["EventRow"]:
        """Most recent reconciliation_events rows, newest first."""
        with Session(self._engine) as session:
            rows = session.query(EventRow).order_by(
                EventRow.created_at.desc(),
            ).limit(limit).all()
            for r in rows:
                session.expunge(r)
            return rows

    def events_for_strike(
        self, strike: "StrikeRow", limit: int = 20,
    ) -> list["EventRow"]:
        """Recent events that share this strike's symbol (and strategy_id, if set).

        For qty_drift / broker_only strikes (strategy_id=None), filters by symbol
        only. For mysql_only strikes (strategy_id set), filters by both —
        events with strategy_id=None (e.g. heartbeats) are also included.
        """
        with Session(self._engine) as session:
            q = session.query(EventRow).filter(
                EventRow.symbol == strike.symbol,
            )
            if strike.strategy_id is not None:
                q = q.filter(
                    (EventRow.strategy_id == strike.strategy_id)
                    | (EventRow.strategy_id.is_(None))
                )
            rows = q.order_by(EventRow.created_at.desc()).limit(limit).all()
            for r in rows:
                session.expunge(r)
            return rows

    def insert_adopted_position(
        self,
        strategy_id: int,
        setup_name: str,
        symbol: str,
        side: str,
        qty: float,
        entry_px: float,
        opened_at: datetime,
        asset_class: str,
        client_order_id: str,
    ) -> int:
        """Insert a position row for an operator-adopted broker_only orphan.

        Same shape as insert_position_from_fill but with adopted=True.
        Used by `scripts/reconcile_resolve.py adopt`.
        """
        with Session(self._engine) as session:
            row = PositionRow(
                strategy_id=strategy_id,
                symbol=self._normalize_symbol(symbol),
                asset_class=asset_class,
                side=side,
                qty=Decimal(str(qty)),
                entry_px=Decimal(str(entry_px)),
                stop_px=None,
                target_px=None,
                initial_stop_px=None,
                setup_name=setup_name,
                order_id="",
                client_order_id=client_order_id,
                stop_order_id=None,
                breakeven_moved=False,
                bars_held=0,
                adopted=True,
                # Adopted from a real broker_only orphan — the broker
                # actually holds it. Engine should manage virtual exits
                # immediately.
                fill_confirmed=True,
                exit_submitted=False,
                status="open",
                opened_at=opened_at,
            )
            session.add(row)
            session.commit()
            session.refresh(row)
            self._log.info(
                "MYSQL_OPERATOR_ADOPTED strategy_id=%d symbol=%s setup=%s qty=%s coid=%s",
                strategy_id, symbol, setup_name, qty, client_order_id,
            )
            return row.id

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

    # ------------------------------------------------------------------
    # broker_credentials CRUD
    # ------------------------------------------------------------------

    def get_broker_credentials(self, asset_class: str) -> dict | None:
        """Return a dict with keys api_key, secret_key, base_url, account_number,
        updated_at — or None if the row is missing or has empty key/secret."""
        with Session(self._engine) as sess:
            row = sess.get(BrokerCredentialsRow, asset_class)
            if row is None:
                return None
            if not row.api_key or not row.secret_key:
                return None
            return {
                "asset_class": row.asset_class,
                "api_key": row.api_key,
                "secret_key": row.secret_key,
                "base_url": row.base_url,
                "account_number": row.account_number,
                "updated_at": row.updated_at,
            }

    def upsert_broker_credentials(
        self,
        asset_class: str,
        api_key: str,
        secret_key: str,
        base_url: str,
    ) -> None:
        """Insert or update credentials for the asset class. Resets account_number
        to NULL — caller should re-test the connection and set it via
        set_broker_credentials_account_number."""
        now = datetime.now(timezone.utc)
        with Session(self._engine) as sess:
            row = sess.get(BrokerCredentialsRow, asset_class)
            if row is None:
                row = BrokerCredentialsRow(
                    asset_class=asset_class,
                    api_key=api_key,
                    secret_key=secret_key,
                    base_url=base_url,
                    account_number=None,
                    updated_at=now,
                )
                sess.add(row)
            else:
                row.api_key = api_key
                row.secret_key = secret_key
                row.base_url = base_url
                row.account_number = None
                row.updated_at = now
            sess.commit()

    def set_broker_credentials_account_number(
        self, asset_class: str, account_number: str,
    ) -> None:
        """Cache the Alpaca account number after a successful test_connection."""
        with Session(self._engine) as sess:
            row = sess.get(BrokerCredentialsRow, asset_class)
            if row is None:
                return
            row.account_number = account_number
            row.updated_at = datetime.now(timezone.utc)
            sess.commit()
