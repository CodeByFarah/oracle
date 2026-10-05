"""Ingestion: store raw bytes first, then normalize and commit a new dataset version.

Raw payloads are persisted before normalization, so a mapping bug never loses source data and
any dataset version can be rebuilt from `raw/`.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from oracle.data.lake import Lake, RawRef
from oracle.data.manifest import DatasetManifest
from oracle.data.providers.base import DataProvider, RawPayload
from oracle.normalization.payloads import normalize_payloads
from oracle.schemas.market import CONTRACTS, DatasetKind


@dataclass(frozen=True)
class IngestResult:
    dataset: DatasetKind
    raw: tuple[RawRef, ...]
    rows_normalized: int
    exact_duplicates_dropped: int
    manifest: DatasetManifest | None
    changed: bool


def ingest_payloads(
    payloads: Sequence[RawPayload],
    provider: DataProvider,
    lake: Lake,
    *,
    now: datetime,
) -> IngestResult:
    kinds = {p.dataset for p in payloads}
    if len(kinds) != 1:
        raise ValueError(f"ingest needs payloads for exactly one dataset, got {sorted(kinds)}")
    kind = kinds.pop()
    contract = CONTRACTS[kind]

    raw_refs = tuple(lake.put_raw(p) for p in payloads)
    normalized = normalize_payloads(payloads, provider.to_canonical, contract)

    parent = lake.head(kind)
    if normalized.table.num_rows == 0:
        return IngestResult(kind, raw_refs, 0, normalized.exact_duplicates_dropped, parent, False)

    manifest = lake.commit(
        contract, normalized.table, sources=[r.sha256 for r in raw_refs], created_at=now
    )
    changed = parent is None or manifest.version != parent.version
    return IngestResult(
        kind,
        raw_refs,
        normalized.table.num_rows,
        normalized.exact_duplicates_dropped,
        manifest,
        changed,
    )
