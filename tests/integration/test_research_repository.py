"""Ledger, hypothesis registry and journal against a throwaway PostgreSQL database."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

from oracle.discovery.calibration import PlantedEffect, calibration_data
from oracle.discovery.engine import ResearchProtocol, run_discovery
from oracle.hypothesis.model import HypothesisStatus, hypothesis_from_tests
from oracle.hypothesis.repository import (
    JournalEntry,
    add_journal_entry,
    count_tests,
    load_hypothesis,
    register_hypothesis,
    save_discovery,
    search_journal,
    update_hypothesis_status,
)
from tests.integration.test_registry_migrations import (  # noqa: F401 - pytest fixture
    REPO_ROOT,
    scratch_db_url,
)

pytestmark = pytest.mark.integration


def test_ledger_hypotheses_and_journal(scratch_db_url: str) -> None:  # noqa: F811
    command.upgrade(Config(str(REPO_ROOT / "alembic.ini")), "head")
    engine = create_engine(scratch_db_url)
    protocol = ResearchProtocol(name="it", horizons=(1, 5), symbol_variables=("f1", "f2"))
    data = calibration_data(
        n_timestamps=2000,
        n_symbols=8,
        symbol_variables=("f1", "f2"),
        state_variables=(),
        horizons=(1, 5),
        planted=PlantedEffect("f1", "high", 0.0004),
        seed=1,
    )
    result = run_discovery(data, protocol, dataset_version="ds-x", feature_set_id="fs-x")
    inserted = save_discovery(engine, result)
    assert inserted == len(result.tests) + len(result.validation_tests)
    assert save_discovery(engine, result) == 0  # idempotent
    assert count_tests(engine, protocol.protocol_hash) == inserted

    survivor = result.test(result.validated[0])
    validation = next(v for v in result.validation_tests if v.candidate == survivor.candidate)
    h = hypothesis_from_tests(
        survivor,
        validation,
        p_adjusted=result.p_adjusted[survivor.test_id],
        dataset_version="ds-x",
        feature_set_id="fs-x",
        created_at=datetime(2026, 10, 5, tzinfo=UTC),
    )
    first = register_hypothesis(engine, h, actor="test")
    second = register_hypothesis(engine, h, actor="test")
    assert first.hypothesis_id == "H-0000001"
    assert second.hypothesis_id == "H-0000002"
    assert load_hypothesis(engine, "H-0000001") == first
    moved = update_hypothesis_status(
        engine, first, HypothesisStatus.SURVIVED_ADVERSARIAL, actor="test", reason="unit test"
    )
    assert load_hypothesis(engine, "H-0000001").status is moved.status

    entry = JournalEntry(
        author="test",
        question="Does f1 predict 1-bar excess returns?",
        hypothesis=first.statement,
        method="calibration discovery run",
        data="synthetic calibration table",
        result="validated",
        interpretation="planted effect recovered",
        failures="none",
        next_experiment="adversarial lab",
        hypothesis_id=first.hypothesis_id,
        tags=("calibration",),
    )
    entry_id = add_journal_entry(engine, entry)
    assert [hit["entry_id"] for hit in search_journal(engine, "calibration discovery")] == [
        entry_id
    ]
    assert search_journal(engine, "volatility regime") == []
    with engine.connect() as conn:
        audits = conn.execute(
            text("SELECT count(*) FROM audit_log WHERE subject_type = 'research'")
        ).scalar_one()
    assert audits == 4  # two registrations, one status change, one journal entry
    engine.dispose()
