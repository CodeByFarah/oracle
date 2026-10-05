"""Dataset manifests: the identity of a dataset version.

A version is the SHA-256 of (dataset, schema_version, partition -> file hash). Two lakes holding
byte-identical partitions therefore share a version id, whatever their history or creation time.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from oracle.common.hashing import sha256_json
from oracle.schemas.market import DatasetKind

_HEX64 = r"^[0-9a-f]{64}$"
VERSION_PREFIX = "ds-"


class FileEntry(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str  # POSIX path relative to the lake root
    sha256: str = Field(pattern=_HEX64)
    rows: int = Field(ge=0)
    bytes: int = Field(ge=0)


class DatasetManifest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    dataset: DatasetKind
    schema_version: int = Field(ge=1)
    partitions: dict[str, FileEntry]
    sources: tuple[str, ...]  # sha256 of every raw payload that contributed, sorted
    parent_version: str | None
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("created_at must be timezone-aware")
        return value

    @property
    def version(self) -> str:
        identity = {
            "dataset": self.dataset.value,
            "schema_version": self.schema_version,
            "partitions": {key: entry.sha256 for key, entry in sorted(self.partitions.items())},
        }
        return VERSION_PREFIX + sha256_json(identity)

    @property
    def row_count(self) -> int:
        return sum(entry.rows for entry in self.partitions.values())
