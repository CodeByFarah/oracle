"""Canonical dataset contracts: Arrow schema, primary key and partitioning per dataset.

Conventions shared by every contract:
- All timestamps are `timestamp[ns, tz=UTC]`. Naive timestamps are rejected at conform time.
- `event_ts` is when something happened; `available_ts` is the earliest moment ORACLE could
  have known it. Point-in-time research must filter on `available_ts`, never `event_ts`.
- `is_synthetic` is carried on every row so generated data can never be mistaken for market data.
- Prices are stored unadjusted. Adjustments are derived views (Phase 3+).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

import pyarrow as pa

UTC_NS = pa.timestamp("ns", tz="UTC")


def _schema(*fields: pa.Field[Any]) -> pa.Schema:
    return pa.schema(list(fields))


_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def to_datetimes(column: pa.Array[Any] | pa.ChunkedArray[Any]) -> list[datetime]:
    """UTC datetimes (microsecond precision) from a timestamp column.

    Faster than `to_pylist()` on tz-aware columns, which on this platform attempts a slow
    module import per element.
    """
    nanos = column.cast(pa.int64()).to_pylist()
    if None in nanos:
        raise ValueError("to_datetimes requires a column without nulls")
    return [_EPOCH + timedelta(microseconds=n // 1000) for n in nanos if n is not None]


class DatasetKind(StrEnum):
    BARS = "bars"
    MACRO_OBSERVATIONS = "macro_observations"
    FILINGS = "filings"


class PartitionTransform(StrEnum):
    IDENTITY = "identity"
    UTC_MONTH = "utc_month"  # timestamp column -> "YYYY-MM"


@dataclass(frozen=True)
class PartitionField:
    column: str
    transform: PartitionTransform = PartitionTransform.IDENTITY


@dataclass(frozen=True)
class DatasetContract:
    kind: DatasetKind
    schema_version: int
    schema: pa.Schema
    primary_key: tuple[str, ...]
    partition_by: tuple[PartitionField, ...]


BARS = DatasetContract(
    kind=DatasetKind.BARS,
    schema_version=1,
    schema=_schema(
        pa.field("source", pa.string(), nullable=False),
        pa.field("symbol", pa.string(), nullable=False),
        pa.field("interval", pa.string(), nullable=False),
        pa.field("event_ts", UTC_NS, nullable=False),  # bar open
        pa.field("available_ts", UTC_NS, nullable=False),  # bar close
        pa.field("open", pa.float64(), nullable=False),
        pa.field("high", pa.float64(), nullable=False),
        pa.field("low", pa.float64(), nullable=False),
        pa.field("close", pa.float64(), nullable=False),
        pa.field("volume", pa.float64(), nullable=False),
        pa.field("trade_count", pa.int64(), nullable=True),
        pa.field("is_synthetic", pa.bool_(), nullable=False),
    ),
    primary_key=("source", "symbol", "interval", "event_ts"),
    partition_by=(
        PartitionField("source"),
        PartitionField("interval"),
        PartitionField("symbol"),
        PartitionField("event_ts", PartitionTransform.UTC_MONTH),
    ),
)

MACRO_OBSERVATIONS = DatasetContract(
    kind=DatasetKind.MACRO_OBSERVATIONS,
    schema_version=1,
    schema=_schema(
        pa.field("source", pa.string(), nullable=False),
        pa.field("series_id", pa.string(), nullable=False),
        pa.field("observation_date", pa.date32(), nullable=False),  # reference period
        pa.field("value", pa.float64(), nullable=True),  # null = missing in source
        pa.field("realtime_start", pa.date32(), nullable=False),  # vintage valid from
        pa.field("realtime_end", pa.date32(), nullable=False),  # vintage valid to
        pa.field("available_ts", UTC_NS, nullable=False),
        pa.field("is_synthetic", pa.bool_(), nullable=False),
    ),
    primary_key=("source", "series_id", "observation_date", "realtime_start"),
    partition_by=(PartitionField("source"), PartitionField("series_id")),
)

FILINGS = DatasetContract(
    kind=DatasetKind.FILINGS,
    schema_version=1,
    schema=_schema(
        pa.field("source", pa.string(), nullable=False),
        pa.field("cik", pa.string(), nullable=False),  # 10-digit zero-padded
        pa.field("accession_number", pa.string(), nullable=False),
        pa.field("form", pa.string(), nullable=False),
        pa.field("filing_date", pa.date32(), nullable=False),
        pa.field("report_date", pa.date32(), nullable=True),
        pa.field("event_ts", UTC_NS, nullable=False),  # EDGAR acceptance time
        pa.field("available_ts", UTC_NS, nullable=False),
        pa.field("primary_document", pa.string(), nullable=True),
        pa.field("is_synthetic", pa.bool_(), nullable=False),
    ),
    # An accession can be filed for several co-registrants, so CIK is part of the key.
    primary_key=("source", "cik", "accession_number"),
    partition_by=(PartitionField("source"), PartitionField("cik")),
)

CONTRACTS: dict[DatasetKind, DatasetContract] = {
    c.kind: c for c in (BARS, MACRO_OBSERVATIONS, FILINGS)
}
