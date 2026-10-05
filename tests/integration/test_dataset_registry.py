"""Dataset-version registration against a throwaway PostgreSQL database."""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

from oracle.data.lake import Lake
from oracle.ingestion.pipeline import ingest_payloads
from oracle.registry.datasets import register_manifest, registered_versions
from tests.integration.test_registry_migrations import (  # noqa: F401 - pytest fixture
    REPO_ROOT,
    scratch_db_url,
)
from tests.unit.data.helpers import NOW, synthetic_payloads

pytestmark = pytest.mark.integration


def test_register_manifest_is_idempotent_and_audited(scratch_db_url: str, tmp_path: Path) -> None:  # noqa: F811
    command.upgrade(Config(str(REPO_ROOT / "alembic.ini")), "head")
    provider, payloads = synthetic_payloads()
    manifest = ingest_payloads(payloads, provider, Lake(tmp_path), now=NOW).manifest
    assert manifest is not None

    engine = create_engine(scratch_db_url)
    assert register_manifest(engine, manifest, actor="test") is True
    assert register_manifest(engine, manifest, actor="test") is False
    assert registered_versions(engine, "bars") == [manifest.version]
    with engine.connect() as conn:
        audit_rows = conn.execute(
            text("SELECT count(*) FROM audit_log WHERE action = 'register_dataset_version'")
        ).scalar_one()
        stored_rows = conn.execute(
            text("SELECT row_count FROM dataset_versions WHERE version = :v"),
            {"v": manifest.version},
        ).scalar_one()
    assert audit_rows == 1
    assert stored_rows == manifest.row_count
    engine.dispose()
