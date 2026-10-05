from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from oracle.cli.main import app
from oracle.common.config import get_settings

runner = CliRunner()
INGEST = ["ingest", "synthetic", "--symbols", "AAA", "--start", "2024-01-02", "--end", "2024-01-02"]


@pytest.fixture(autouse=True)
def _isolated_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.chdir(tmp_path)  # no .env here
    monkeypatch.setenv("ORACLE_DATA_ROOT", str(tmp_path / "lake"))
    monkeypatch.delenv("ORACLE_DATABASE_URL", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_providers_lists_status_and_reasons() -> None:
    result = runner.invoke(app, ["providers"])
    assert result.exit_code == 0
    assert "synthetic  enabled" in result.output
    assert "SYNTHETIC" in result.output
    assert "databento  disabled" in result.output
    assert "paid" in result.output


def test_synthetic_ingest_show_and_verify() -> None:
    first = runner.invoke(app, INGEST)
    assert first.exit_code == 0, first.output
    assert "390 rows normalized" in first.output
    assert "new version: ds-" in first.output

    again = runner.invoke(app, INGEST)
    assert "unchanged: ds-" in again.output

    shown = runner.invoke(app, ["dataset", "show", "bars"])
    assert "rows            390" in shown.output
    verified = runner.invoke(app, ["dataset", "verify", "bars"])
    assert verified.exit_code == 0
    assert verified.output.startswith("OK ds-")


def test_conflicting_reingest_fails_cleanly() -> None:
    assert runner.invoke(app, INGEST).exit_code == 0
    result = runner.invoke(app, [*INGEST, "--seed", "99"])
    assert result.exit_code == 2
    assert "conflicting rows" in result.output


def test_disabled_provider_fails_cleanly() -> None:
    result = runner.invoke(app, ["ingest", "fred", "--series", "DGS10"])
    assert result.exit_code == 2
    assert "FRED_API_KEY not set" in result.output


def test_missing_dataset_fails_cleanly() -> None:
    result = runner.invoke(app, ["dataset", "verify", "filings"])
    assert result.exit_code == 2
