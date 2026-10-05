"""SQLAlchemy engine factory for the metadata database."""

from __future__ import annotations

from sqlalchemy import Engine, create_engine

from oracle.common.config import Settings


def make_engine(settings: Settings) -> Engine:
    return create_engine(settings.require_database_url(), pool_pre_ping=True)
