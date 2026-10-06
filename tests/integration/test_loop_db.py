"""Research loop persisting its ledger and journal to a throwaway PostgreSQL database."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine

from oracle.data.lake import Lake
from oracle.hypothesis.repository import count_tests, search_journal
from oracle.ingestion.pipeline import ingest_payloads
from oracle.loop.research_loop import LoopConfig, run_once
from tests.integration.test_registry_migrations import (  # noqa: F401 - pytest fixture
    REPO_ROOT,
    scratch_db_url,
)
from tests.unit.data.helpers import synthetic_payloads

pytestmark = pytest.mark.integration


def test_loop_persists_ledger_and_journal(scratch_db_url: str, tmp_path: Path) -> None:  # noqa: F811
    command.upgrade(Config(str(REPO_ROOT / "alembic.ini")), "head")
    engine = create_engine(scratch_db_url)
    lake = Lake(tmp_path / "lake")
    now = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
    provider, payloads = synthetic_payloads(
        symbols=("AAA", "BBB", "CCC", "DDD"),
        start=datetime(2024, 1, 2, tzinfo=UTC),
        end=datetime(2024, 1, 10, tzinfo=UTC),
    )
    manifest = ingest_payloads(payloads, provider, lake, now=now).manifest
    assert manifest is not None
    config = LoopConfig(research_budget=1, lab_permutations=20)
    report = run_once(lake, manifest, now=now, config=config, engine=engine)
    assert count_tests(engine) >= report.tests_run
    assert search_journal(engine, "extremes")
    engine.dispose()
