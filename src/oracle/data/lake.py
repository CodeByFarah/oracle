"""Local data lake: content-addressed raw payloads + copy-on-write Parquet datasets.

Layout under the lake root:
    raw/{provider}/{dataset}/{sha[:2]}/{sha}{ext}           exact bytes from the source
    raw/{provider}/{dataset}/{sha[:2]}/{sha}.meta.json      retrieval metadata (no secrets)
    normalized/{dataset}/v{schema}/{partition}/part-{sha16}.parquet
    manifests/{dataset}/{version}.json
    manifests/{dataset}/HEAD                                latest version id

Files are never modified after they are written. Committing new rows writes new partition
files and a new manifest; older manifests stay valid and reproducible.
Concurrency: single writer per lake. Writes are atomic (temp file + rename).
"""

from __future__ import annotations

import json
import os
import secrets
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Final, Literal
from urllib.parse import quote

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from oracle.common.hashing import canonical_json, sha256_bytes, sha256_file
from oracle.data.manifest import DatasetManifest, FileEntry
from oracle.data.providers.base import RawPayload
from oracle.schemas.conform import conform
from oracle.schemas.market import DatasetContract, DatasetKind, PartitionTransform

_EXTENSIONS = {
    "application/json": ".json",
    "text/csv": ".csv",
    "application/x-dbn": ".dbn.zst",
}
_KEY_SEPARATOR = "\x1f"
_PARQUET_COMPRESSION: Final = "zstd"


class LakeIntegrityError(RuntimeError):
    """Stored bytes do not match their recorded hash, or the lake state is inconsistent."""


@dataclass(frozen=True)
class RawRef:
    provider: str
    dataset: DatasetKind
    sha256: str
    path: str
    bytes: int


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp-{secrets.token_hex(4)}")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def _partition_labels(contract: DatasetContract) -> list[str]:
    return [
        f.column if f.transform is PartitionTransform.IDENTITY else f"{f.column}_month"
        for f in contract.partition_by
    ]


def split_partitions(table: pa.Table, contract: DatasetContract) -> list[tuple[str, pa.Table]]:
    """Split a conformed table into (partition path, rows) pairs, ordered by partition path."""
    if table.num_rows == 0:
        return []
    key_parts: list[Any] = []
    for field in contract.partition_by:
        column: Any = table[field.column]
        if field.transform is PartitionTransform.UTC_MONTH:
            # Dropping the tz label keeps the UTC instant and avoids needing a tz database.
            key_parts.append(pc.strftime(column.cast(pa.timestamp("ns")), format="%Y-%m"))
        else:
            key_parts.append(column.cast(pa.string()))
    keys = pc.binary_join_element_wise(*key_parts, _KEY_SEPARATOR)
    order = pc.sort_indices(keys)
    sorted_table = table.take(order)
    counts = pc.value_counts(keys.take(order))
    labels = _partition_labels(contract)
    primary_key_order: list[tuple[str, Literal["ascending", "descending"]]] = [
        (k, "ascending") for k in contract.primary_key
    ]
    result: list[tuple[str, pa.Table]] = []
    offset = 0
    for item in counts:
        values = str(item["values"].as_py()).split(_KEY_SEPARATOR)
        n = int(item["counts"].as_py())
        path = "/".join(
            f"{label}={quote(v, safe='')}" for label, v in zip(labels, values, strict=True)
        )
        result.append((path, sorted_table.slice(offset, n).sort_by(primary_key_order)))
        offset += n
    return result


def _parquet_bytes(table: pa.Table) -> bytes:
    sink = pa.BufferOutputStream()
    pq.write_table(table.combine_chunks(), sink, compression=_PARQUET_COMPRESSION)
    return bytes(sink.getvalue().to_pybytes())


class Lake:
    def __init__(self, root: Path) -> None:
        self.root = root

    def _abs(self, relative: str) -> Path:
        return self.root.joinpath(*relative.split("/"))

    def resolve(self, relative: str) -> Path:
        """Absolute path of a lake-relative POSIX path (as stored in manifests and RawRefs)."""
        return self._abs(relative)

    def raw_payload(self, provider: str, kind: DatasetKind, sha256: str) -> RawPayload:
        """Reconstruct a stored payload from its bytes and metadata, verifying the hash."""
        base = f"raw/{provider}/{kind.value}/{sha256[:2]}/{sha256}"
        meta = json.loads(self._abs(base + ".meta.json").read_bytes())
        ext = _EXTENSIONS.get(meta["content_type"], ".bin")
        ref = RawRef(provider, kind, sha256, base + ext, 0)
        return RawPayload(
            provider=provider,
            dataset=kind,
            content=self.read_raw(ref),
            content_type=meta["content_type"],
            retrieved_at=datetime.fromisoformat(meta["retrieved_at"]),
            request=tuple(sorted(meta["request"].items())),
        )

    def raw_providers(self, kind: DatasetKind) -> dict[str, str]:
        """Map raw payload sha256 -> provider for every stored payload of a dataset."""
        found: dict[str, str] = {}
        raw_root = self._abs("raw")
        if not raw_root.exists():
            return found
        for meta_path in raw_root.glob(f"*/{kind.value}/*/*.meta.json"):
            found[meta_path.name.removesuffix(".meta.json")] = meta_path.parents[2].name
        return found

    # ---- raw -----------------------------------------------------------------------------

    def put_raw(self, payload: RawPayload) -> RawRef:
        sha = sha256_bytes(payload.content)
        ext = _EXTENSIONS.get(payload.content_type, ".bin")
        base = f"raw/{payload.provider}/{payload.dataset.value}/{sha[:2]}/{sha}"
        relative = base + ext
        target = self._abs(relative)
        if target.exists():
            if sha256_file(target) != sha:
                raise LakeIntegrityError(f"raw payload corrupted on disk: {relative}")
        else:
            _atomic_write(target, payload.content)
            meta = {
                "provider": payload.provider,
                "dataset": payload.dataset.value,
                "sha256": sha,
                "content_type": payload.content_type,
                "retrieved_at": payload.retrieved_at.isoformat(),
                "request": dict(payload.request),
            }
            _atomic_write(self._abs(base + ".meta.json"), canonical_json(meta))
        return RawRef(payload.provider, payload.dataset, sha, relative, len(payload.content))

    def read_raw(self, ref: RawRef) -> bytes:
        data = self._abs(ref.path).read_bytes()
        if sha256_bytes(data) != ref.sha256:
            raise LakeIntegrityError(f"raw payload hash mismatch: {ref.path}")
        return data

    # ---- manifests -----------------------------------------------------------------------

    def _manifest_dir(self, kind: DatasetKind) -> Path:
        return self._abs(f"manifests/{kind.value}")

    def head(self, kind: DatasetKind) -> DatasetManifest | None:
        head_file = self._manifest_dir(kind) / "HEAD"
        if not head_file.exists():
            return None
        return self.load_manifest(kind, head_file.read_text(encoding="utf-8").strip())

    def load_manifest(self, kind: DatasetKind, version: str) -> DatasetManifest:
        path = self._manifest_dir(kind) / f"{version}.json"
        manifest = DatasetManifest.model_validate_json(path.read_bytes())
        if manifest.version != version or manifest.dataset is not kind:
            raise LakeIntegrityError(f"manifest {path} does not match its version id")
        return manifest

    def _write_manifest(self, manifest: DatasetManifest) -> None:
        directory = self._manifest_dir(manifest.dataset)
        body = canonical_json(manifest.model_dump(mode="json"))
        _atomic_write(directory / f"{manifest.version}.json", body)
        _atomic_write(directory / "HEAD", manifest.version.encode("utf-8"))

    # ---- normalized datasets -------------------------------------------------------------

    def _read_entry(self, entry: FileEntry, *, verify: bool) -> pa.Table:
        path = self._abs(entry.path)
        if verify and sha256_file(path) != entry.sha256:
            raise LakeIntegrityError(f"partition file hash mismatch: {entry.path}")
        # ParquetFile reads exactly this file; read_table would also infer Hive partition
        # columns from the key=value directory names.
        return pq.ParquetFile(path).read()

    def _write_partition(self, contract: DatasetContract, key: str, table: pa.Table) -> FileEntry:
        data = _parquet_bytes(table)
        sha = sha256_bytes(data)
        relative = (
            f"normalized/{contract.kind.value}/v{contract.schema_version}/{key}/"
            f"part-{sha[:16]}.parquet"
        )
        target = self._abs(relative)
        if not target.exists():
            _atomic_write(target, data)
        elif sha256_file(target) != sha:
            raise LakeIntegrityError(f"existing partition file corrupted: {relative}")
        return FileEntry(path=relative, sha256=sha, rows=table.num_rows, bytes=len(data))

    def commit(
        self,
        contract: DatasetContract,
        table: pa.Table,
        *,
        sources: Iterable[str],
        created_at: datetime,
    ) -> DatasetManifest:
        """Merge rows into the dataset and return the resulting manifest (HEAD).

        Rows whose primary key already exists with different values raise
        DuplicateKeyConflictError: revisions are never resolved silently.
        """
        new_rows = conform(table, contract).table
        if new_rows.num_rows == 0:
            raise ValueError("nothing to commit: table is empty")
        parent = self.head(contract.kind)
        if parent is not None and parent.schema_version != contract.schema_version:
            raise LakeIntegrityError(
                f"{contract.kind}: lake has schema v{parent.schema_version}, contract is "
                f"v{contract.schema_version}; a migration is required"
            )
        partitions = dict(parent.partitions) if parent else {}
        for key, rows in split_partitions(new_rows, contract):
            merged = rows
            if key in partitions:
                existing = self._read_entry(partitions[key], verify=True)
                merged = conform(pa.concat_tables([existing, rows]), contract).table
            partitions[key] = self._write_partition(contract, key, merged)

        all_sources = sorted(set(parent.sources if parent else ()) | set(sources))
        manifest = DatasetManifest(
            dataset=contract.kind,
            schema_version=contract.schema_version,
            partitions=partitions,
            sources=tuple(all_sources),
            parent_version=parent.version if parent else None,
            created_at=created_at,
        )
        if parent is not None and manifest.version == parent.version:
            return parent  # identical content: no new version
        self._write_manifest(manifest)
        return manifest

    def read(self, manifest: DatasetManifest, *, verify: bool = True) -> pa.Table:
        tables = [
            self._read_entry(entry, verify=verify)
            for _, entry in sorted(manifest.partitions.items())
        ]
        return pa.concat_tables(tables) if tables else pa.table({})

    def verify(self, manifest: DatasetManifest) -> list[str]:
        """Return a list of integrity problems (empty means the version is intact)."""
        problems: list[str] = []
        for key, entry in sorted(manifest.partitions.items()):
            path = self._abs(entry.path)
            if not path.exists():
                problems.append(f"{key}: missing file {entry.path}")
                continue
            if sha256_file(path) != entry.sha256:
                problems.append(f"{key}: hash mismatch {entry.path}")
                continue
            rows = pq.ParquetFile(path).metadata.num_rows
            if rows != entry.rows:
                problems.append(f"{key}: expected {entry.rows} rows, found {rows}")
        return problems
