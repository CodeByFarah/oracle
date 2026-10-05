"""Alembic environment. The URL comes from ORACLE_DATABASE_URL, never from alembic.ini."""

from __future__ import annotations

from alembic import context
from sqlalchemy import create_engine

from oracle.common.config import Settings
from oracle.registry.models import Base

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=Settings().require_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(Settings().require_database_url())
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
