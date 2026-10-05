"""Rebuild a dataset version from its stored raw payloads and check it is reproduced exactly.

This is the end-to-end reproducibility check for ingestion: raw bytes -> normalizers ->
conform -> partitioned Parquet must yield the same content-hashed version id.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import pyarrow as pa

from oracle.data.lake import Lake
from oracle.data.manifest import DatasetManifest
from oracle.data.providers.registry import CANONICALIZERS
from oracle.normalization.payloads import normalize_payloads
from oracle.schemas.conform import conform
from oracle.schemas.market import CONTRACTS


class RebuildError(RuntimeError):
    pass


@dataclass(frozen=True)
class RebuildResult:
    original_version: str
    rebuilt_version: str
    rows: int

    @property
    def reproduced(self) -> bool:
        return self.original_version == self.rebuilt_version


def rebuild(
    lake: Lake, manifest: DatasetManifest, scratch_root: Path, *, now: datetime
) -> RebuildResult:
    """Rebuild `manifest` from raw payloads into an empty lake at `scratch_root`."""
    contract = CONTRACTS[manifest.dataset]
    owners = lake.raw_providers(manifest.dataset)
    if missing := [s for s in manifest.sources if s not in owners]:
        raise RebuildError(f"{len(missing)} raw payloads missing, e.g. {missing[0]}")

    by_provider: dict[str, list[str]] = {}
    for sha in manifest.sources:
        by_provider.setdefault(owners[sha], []).append(sha)

    tables = []
    for provider, shas in sorted(by_provider.items()):
        if provider not in CANONICALIZERS:
            raise RebuildError(f"no normalizer registered for provider {provider!r}")
        payloads = [lake.raw_payload(provider, manifest.dataset, sha) for sha in shas]
        tables.append(normalize_payloads(payloads, CANONICALIZERS[provider], contract).table)

    combined = conform(pa.concat_tables(tables), contract).table
    scratch = Lake(scratch_root)
    if scratch.head(manifest.dataset) is not None:
        raise RebuildError(f"scratch lake {scratch_root} is not empty")
    rebuilt = scratch.commit(contract, combined, sources=manifest.sources, created_at=now)
    return RebuildResult(manifest.version, rebuilt.version, rebuilt.row_count)
