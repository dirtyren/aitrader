"""Shared SQLAlchemy engine for read-only dashboard queries.

Reads the same MYSQL_* env vars as state/mysql_store.py so the
dashboard container picks up the docker-compose configuration
unchanged.
"""
from __future__ import annotations

import os
from urllib.parse import quote_plus as urlquote

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

_engine: Engine | None = None


def get_engine() -> Engine:
    """Return a process-wide SQLAlchemy engine (lazily created)."""
    global _engine
    if _engine is None:
        host = os.environ.get("MYSQL_HOST", "mysql")
        port = os.environ.get("MYSQL_PORT", "3306")
        user = os.environ.get("MYSQL_USER", "trader")
        password = os.environ.get("MYSQL_PASSWORD", "traderpass")
        database = os.environ.get("MYSQL_DATABASE", "aitrader")
        url = f"mysql+pymysql://{user}:{urlquote(password)}@{host}:{port}/{database}"
        _engine = create_engine(
            url,
            pool_pre_ping=True,
            pool_recycle=3600,
            connect_args={"connect_timeout": 5},
        )
    return _engine
