"""Provenance: the minimum record every research result must carry to be reproducible.

`ProvenanceRecord.fingerprint()` hashes only the inputs that determine a result
(code, data, features, model, universe, costs, protocol). Two runs with equal
fingerprints must produce identical outputs; the reproducibility tests rely on that.
"""

from __future__ import annotations

import subprocess
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator

from oracle.common.hashing import sha256_json

_HEX40 = r"^[0-9a-f]{40}$"
_NON_EMPTY = Field(min_length=1)


class CodeVersionError(RuntimeError):
    """The code version cannot be determined (not a git checkout, or no commits yet)."""


class CodeVersion(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    commit: str = Field(pattern=_HEX40)
    dirty: bool


class ProvenanceRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    experiment_id: str = _NON_EMPTY
    created_at: datetime
    code_version: CodeVersion
    data_version: str = _NON_EMPTY
    feature_version: str = _NON_EMPTY
    # None means the result involves no fitted model (e.g. a pure conditional event study).
    model_version: str | None = None
    universe_definition: str = _NON_EMPTY
    transaction_cost_assumptions: str = _NON_EMPTY
    protocol_hash: str | None = None

    @field_validator("created_at")
    @classmethod
    def _require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("created_at must be timezone-aware")
        return value

    def fingerprint(self) -> str:
        """Hash of result-determining inputs; excludes experiment_id and created_at."""
        payload = self.model_dump(mode="json", exclude={"experiment_id", "created_at"})
        return sha256_json(payload)


def get_code_version(repo_root: Path) -> CodeVersion:
    """Read HEAD commit and dirty state from git.

    Research runs that will be recorded as evidence should refuse `dirty=True`
    unless explicitly overridden; that policy is enforced by the experiment runner.
    """
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        raise CodeVersionError(f"cannot determine git code version in {repo_root}") from exc
    return CodeVersion(commit=commit, dirty=bool(status.strip()))
