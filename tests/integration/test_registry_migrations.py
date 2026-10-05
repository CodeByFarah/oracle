"""Runs migrations against a throwaway database; never touches the configured database itself.

Skipped when ORACLE_DATABASE_URL is unset or the server is unreachable.
"""

from __future__ import annotations

import secrets
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError, OperationalError

from oracle.common.config import Settings

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def scratch_db_url(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    base = Settings().database_url
    if base is None:
        pytest.skip("ORACLE_DATABASE_URL not set")
    base_url = make_url(base.get_secret_value())
    admin = create_engine(base_url, isolation_level="AUTOCOMMIT")
    name = f"oracle_test_{secrets.token_hex(4)}"
    try:
        with admin.connect() as conn:
            conn.execute(text(f'CREATE DATABASE "{name}"'))
    except OperationalError as exc:
        admin.dispose()
        pytest.skip(f"PostgreSQL unreachable: {exc.__class__.__name__}")
    url = base_url.set(database=name).render_as_string(hide_password=False)
    monkeypatch.setenv("ORACLE_DATABASE_URL", url)
    try:
        yield url
    finally:
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


def _alembic() -> Config:
    return Config(str(REPO_ROOT / "alembic.ini"))


def test_upgrade_creates_schema_and_downgrade_removes_it(scratch_db_url: str) -> None:
    command.upgrade(_alembic(), "head")
    engine = create_engine(scratch_db_url)
    assert {"audit_log", "experiments"} <= set(inspect(engine).get_table_names())

    command.downgrade(_alembic(), "base")
    assert not {"audit_log", "experiments"} & set(inspect(engine).get_table_names())
    engine.dispose()


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE audit_log SET actor = 'tamper'",
        "DELETE FROM audit_log",
        "TRUNCATE audit_log",
    ],
)
def test_audit_log_is_append_only(scratch_db_url: str, statement: str) -> None:
    command.upgrade(_alembic(), "head")
    engine = create_engine(scratch_db_url)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO audit_log (actor, action, subject_type) "
                "VALUES ('test', 'create', 'experiment')"
            )
        )
    with pytest.raises(DBAPIError, match="append-only"), engine.begin() as conn:
        conn.execute(text(statement))
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM audit_log")).scalar_one() == 1
    engine.dispose()


def test_experiment_status_is_constrained(scratch_db_url: str) -> None:
    command.upgrade(_alembic(), "head")
    engine = create_engine(scratch_db_url)
    with pytest.raises(DBAPIError, match="ck_experiments_status"), engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO experiments (experiment_id, status, dataset_version, "
                "feature_version, code_commit, code_dirty, universe_definition, "
                "transaction_cost_assumptions, provenance_fingerprint, parameters) "
                "VALUES ('EXP-x', 'profitable', 'd', 'f', 'c', false, 'u', 't', 'fp', '{}')"
            )
        )
    engine.dispose()
