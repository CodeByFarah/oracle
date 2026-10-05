"""Normalize raw payloads into one conformed table for a dataset contract."""

from __future__ import annotations

from collections.abc import Callable, Sequence

import pyarrow as pa

from oracle.common.hashing import sha256_bytes
from oracle.data.providers.base import RawPayload
from oracle.schemas.conform import ConformResult, conform
from oracle.schemas.market import DatasetContract


class NormalizationError(RuntimeError):
    """A payload could not be mapped onto its contract. The message identifies the payload."""


def normalize_payloads(
    payloads: Sequence[RawPayload],
    to_canonical: Callable[[RawPayload], pa.Table],
    contract: DatasetContract,
) -> ConformResult:
    tables: list[pa.Table] = []
    dropped = 0
    for payload in payloads:
        if payload.dataset is not contract.kind:
            raise NormalizationError(
                f"payload dataset {payload.dataset} does not match contract {contract.kind}"
            )
        label = f"{payload.provider} payload {sha256_bytes(payload.content)[:12]}"
        try:
            result = conform(to_canonical(payload), contract)
        except Exception as exc:
            raise NormalizationError(f"{label} {dict(payload.request)}: {exc}") from exc
        tables.append(result.table)
        dropped += result.exact_duplicates_dropped
    if not tables:
        return ConformResult(contract.schema.empty_table(), 0)
    combined = conform(pa.concat_tables(tables), contract)
    return ConformResult(combined.table, dropped + combined.exact_duplicates_dropped)
