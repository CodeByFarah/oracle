"""SQL access to pinned dataset versions through DuckDB.

Each view reads exactly the Parquet files listed in one manifest, so a query is tied to a
dataset version and stays reproducible after later commits.
"""

from __future__ import annotations

from collections.abc import Iterable

import duckdb

from oracle.data.lake import Lake, LakeIntegrityError
from oracle.data.manifest import DatasetManifest


def _sql_string(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def connect(
    lake: Lake, manifests: Iterable[DatasetManifest], *, verify: bool = True
) -> duckdb.DuckDBPyConnection:
    """In-memory DuckDB connection with one view per manifest, named after its dataset."""
    con = duckdb.connect(":memory:")
    con.execute("SET TimeZone = 'UTC'")
    seen: set[str] = set()
    for manifest in manifests:
        name = manifest.dataset.value
        if name in seen:
            raise ValueError(f"two manifests for dataset {name!r}")
        seen.add(name)
        if verify and (problems := lake.verify(manifest)):
            raise LakeIntegrityError(f"{manifest.version}: {problems[:3]}")
        files = [
            _sql_string(lake.resolve(e.path).as_posix())
            for _, e in sorted(manifest.partitions.items())
        ]
        if not files:
            raise ValueError(f"manifest {manifest.version} has no files")
        con.execute(
            # name is a DatasetKind value; file paths are escaped by _sql_string.
            f"CREATE VIEW {name} AS SELECT * FROM read_parquet([{', '.join(files)}], "  # noqa: S608
            "hive_partitioning = false, union_by_name = false)"
        )
    return con
