from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from oracle.cli.main import app
from oracle.common.config import get_settings
from oracle.data.lake import Lake
from oracle.data.manifest import DatasetManifest
from oracle.ingestion.pipeline import ingest_payloads
from oracle.loop.research_loop import LoopConfig, LoopReport, run_once
from tests.unit.data.helpers import synthetic_payloads

NOW = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
SYMBOLS = ("AAA", "BBB", "CCC", "DDD", "EEE", "FFF")
CONFIG = LoopConfig(research_budget=2, lab_permutations=20)


def _manifest(lake: Lake) -> DatasetManifest:
    provider, payloads = synthetic_payloads(
        symbols=SYMBOLS,
        start=datetime(2024, 1, 2, tzinfo=UTC),
        end=datetime(2024, 1, 12, tzinfo=UTC),
    )
    manifest = ingest_payloads(payloads, provider, lake, now=NOW).manifest
    assert manifest is not None
    return manifest


def test_loop_cycle_on_null_data_is_honest_and_persisted(lake: Lake) -> None:
    report = run_once(lake, _manifest(lake), now=NOW, config=CONFIG)
    assert report.experiments == 2
    assert report.tests_run > 0
    assert report.validated_hypotheses == 0  # synthetic bars carry no planted effect
    assert report.strategies == ()
    assert any("honest null" in n for n in report.notes)
    assert any("never invoked" in n for n in report.notes)
    assert report.world.provenance["dataset_version"] == report.dataset_version
    saved = list((lake.root / "loop").glob("*.json"))
    assert len(saved) == 1
    assert LoopReport.model_validate_json(saved[0].read_text()) == report
    journal = (lake.root / "research" / "journal.jsonl").read_text().splitlines()
    assert len(journal) == 2


def test_second_cycle_continues_the_research_grid(lake: Lake) -> None:
    manifest = _manifest(lake)
    run_once(lake, manifest, now=NOW, config=CONFIG)
    run_once(lake, manifest, now=datetime(2026, 10, 5, 13, 0, tzinfo=UTC), config=CONFIG)
    results = list((lake.root / "research" / "results").glob("*.json"))
    assert len({p.stem.split("__")[0] for p in results}) == 4  # nothing repeated


@pytest.fixture
def cli_lake(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ORACLE_DATA_ROOT", str(tmp_path / "lake"))
    monkeypatch.delenv("ORACLE_DATABASE_URL", raising=False)
    get_settings.cache_clear()
    yield tmp_path / "lake"
    get_settings.cache_clear()


def test_cli_loop_run_once(cli_lake: Path) -> None:
    runner = CliRunner()
    assert runner.invoke(app, ["loop", "run-once"]).exit_code == 2  # no dataset yet
    _manifest(Lake(cli_lake))
    result = runner.invoke(app, ["loop", "run-once", "--budget", "1"])
    assert result.exit_code == 0, result.output
    assert "experiments 1" in result.output
    assert "live execution is never invoked" in result.output
