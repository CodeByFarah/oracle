from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from oracle.common.provenance import (
    CodeVersion,
    CodeVersionError,
    ProvenanceRecord,
    get_code_version,
)

COMMIT = "a" * 40


def _record(**overrides: object) -> ProvenanceRecord:
    fields: dict[str, object] = {
        "experiment_id": "EXP-20261005T000000-00000000",
        "created_at": datetime(2026, 10, 5, tzinfo=UTC),
        "code_version": CodeVersion(commit=COMMIT, dirty=False),
        "data_version": "ds-sha256:abc",
        "feature_version": "fs-v1",
        "universe_definition": "etf-alpha-v1",
        "transaction_cost_assumptions": "costs-v1",
    }
    fields.update(overrides)
    return ProvenanceRecord.model_validate(fields)


def test_fingerprint_ignores_identity_and_timestamp() -> None:
    a = _record()
    b = _record(
        experiment_id="EXP-20261006T000000-11111111",
        created_at=datetime(2026, 10, 6, tzinfo=UTC),
    )
    assert a.fingerprint() == b.fingerprint()


@pytest.mark.parametrize(
    "override",
    [
        {"data_version": "ds-sha256:def"},
        {"feature_version": "fs-v2"},
        {"model_version": "m-1"},
        {"universe_definition": "etf-alpha-v2"},
        {"transaction_cost_assumptions": "costs-v2"},
        {"protocol_hash": "p-1"},
        {"code_version": CodeVersion(commit=COMMIT, dirty=True)},
        {"code_version": CodeVersion(commit="b" * 40, dirty=False)},
    ],
)
def test_fingerprint_changes_with_any_result_determining_input(
    override: dict[str, object],
) -> None:
    assert _record().fingerprint() != _record(**override).fingerprint()


def test_record_is_frozen_and_rejects_unknown_fields() -> None:
    record = _record()
    with pytest.raises(ValidationError):
        record.data_version = "other"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        _record(surprise="field")


@pytest.mark.parametrize("field", ["data_version", "feature_version", "universe_definition"])
def test_required_versions_must_be_non_empty(field: str) -> None:
    with pytest.raises(ValidationError):
        _record(**{field: ""})


def test_naive_created_at_rejected() -> None:
    with pytest.raises(ValidationError, match="timezone-aware"):
        _record(created_at=datetime(2026, 10, 5))  # noqa: DTZ001


def test_code_version_requires_full_sha() -> None:
    with pytest.raises(ValidationError):
        CodeVersion(commit="abc123", dirty=False)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
        cwd=repo,
        check=True,
        capture_output=True,
    )


def test_get_code_version_detects_commit_and_dirty_state(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-q")
    (tmp_path / "f.txt").write_text("1")
    _git(tmp_path, "add", "f.txt")
    _git(tmp_path, "commit", "-q", "-m", "init")

    clean = get_code_version(tmp_path)
    assert clean.dirty is False
    assert len(clean.commit) == 40

    (tmp_path / "f.txt").write_text("2")
    assert get_code_version(tmp_path).dirty is True


def test_get_code_version_fails_without_commits(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-q")
    with pytest.raises(CodeVersionError):
        get_code_version(tmp_path)
