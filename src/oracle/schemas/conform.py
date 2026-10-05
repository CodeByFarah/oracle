"""Enforce a DatasetContract on an Arrow table.

`conform` is strict by design: adapters must map vendor fields exactly onto the contract.
It never guesses timezones, never performs lossy casts, and never silently resolves two
different values for the same primary key.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pyarrow as pa
import pyarrow.compute as pc

from oracle.schemas.market import DatasetContract


class ContractViolationError(ValueError):
    """The table does not satisfy the dataset contract."""


class DuplicateKeyConflictError(ContractViolationError):
    """Two rows share a primary key but differ in other columns (e.g. vendor revision)."""


@dataclass(frozen=True)
class ConformResult:
    table: pa.Table
    exact_duplicates_dropped: int


def _cast_column(
    name: str, column: pa.ChunkedArray[Any], field: pa.Field[Any]
) -> pa.ChunkedArray[Any]:
    source_type = column.type
    if pa.types.is_timestamp(source_type) and source_type.tz is None:
        raise ContractViolationError(f"column '{name}' is a naive timestamp; timezone is required")
    if source_type == field.type:
        return column
    try:
        return column.cast(field.type, safe=True)
    except (pa.ArrowInvalid, pa.ArrowNotImplementedError, pa.ArrowTypeError) as exc:
        raise ContractViolationError(
            f"column '{name}' cannot be safely cast from {source_type} to {field.type}"
        ) from exc


def _empty(contract: DatasetContract) -> pa.Table:
    return contract.schema.empty_table()


def conform(table: pa.Table, contract: DatasetContract) -> ConformResult:
    schema = contract.schema
    names = set(table.column_names)
    expected = set(schema.names)
    if missing := expected - names:
        raise ContractViolationError(f"{contract.kind}: missing columns {sorted(missing)}")
    if extra := names - expected:
        raise ContractViolationError(f"{contract.kind}: unexpected columns {sorted(extra)}")
    if table.num_rows == 0:
        return ConformResult(_empty(contract), 0)

    columns = []
    for field in schema:
        column = _cast_column(field.name, table.column(field.name), field)
        if not field.nullable and column.null_count:
            raise ContractViolationError(
                f"{contract.kind}: column '{field.name}' has {column.null_count} nulls"
            )
        columns.append(column)
    typed = pa.Table.from_arrays(columns, schema=schema)

    distinct = typed.group_by(schema.names, use_threads=False).aggregate([])
    distinct = pa.Table.from_arrays([distinct.column(n) for n in schema.names], schema=schema)
    dropped = typed.num_rows - distinct.num_rows

    key_counts = distinct.group_by(list(contract.primary_key), use_threads=False).aggregate(
        [([], "count_all")]
    )
    conflicts = key_counts.filter(pc.greater(key_counts["count_all"], pa.scalar(1)))
    if conflicts.num_rows:
        sample = conflicts.slice(0, 3).drop_columns(["count_all"]).to_pylist()
        raise DuplicateKeyConflictError(
            f"{contract.kind}: {conflicts.num_rows} primary keys have conflicting rows; "
            f"examples: {sample}"
        )

    ordered = distinct.sort_by([(k, "ascending") for k in contract.primary_key])
    return ConformResult(ordered.combine_chunks(), dropped)
