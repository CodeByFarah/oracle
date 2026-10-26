"""Ledger database engine. SQLite by default (zero setup); PostgreSQL via migration 0004."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy import Engine, create_engine, event, inspect
from sqlalchemy.orm import Session, sessionmaker

from oracle.common.config import ConfigurationError, Settings
from oracle.fintech.models import FintechBase


def _sqlite_pragmas(dbapi_connection: Any, _record: Any) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=15000")
    cursor.close()


def make_engine(url: str) -> Engine:
    if url.startswith("sqlite"):
        path = url.split("///", 1)[1] if "///" in url else ""
        if path and path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        engine = create_engine(url, connect_args={"check_same_thread": False, "timeout": 15})
        event.listen(engine, "connect", _sqlite_pragmas)
        return engine
    return create_engine(url, pool_pre_ping=True)


def make_fintech_engine(settings: Settings) -> Engine:
    return make_engine(settings.fintech_url())


def ensure_schema(engine: Engine) -> None:
    """SQLite: create missing tables. PostgreSQL: require migration 0004 to have run."""
    if engine.dialect.name == "sqlite":
        FintechBase.metadata.create_all(engine)
        return
    if not inspect(engine).has_table("fin_customers"):
        raise ConfigurationError(
            "fintech tables are missing; run `uv run python -m alembic upgrade head`"
        )


def session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(engine, expire_on_commit=False)
