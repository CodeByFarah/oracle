from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from oracle.data.lake import Lake, LakeIntegrityError
from oracle.data.query import connect
from oracle.ingestion.pipeline import ingest_payloads
from oracle.ingestion.rebuild import RebuildError, rebuild
from oracle.schemas.market import DatasetKind
from tests.unit.data.helpers import NOW, synthetic_payloads

LATER = {"start": datetime(2024, 1, 4, tzinfo=UTC), "end": datetime(2024, 1, 5, tzinfo=UTC)}


def _ingest(lake: Lake, **kwargs: Any) -> None:
    provider, payloads = synthetic_payloads(**kwargs)
    ingest_payloads(payloads, provider, lake, now=NOW)


def _count(lake: Lake, manifest: Any) -> int:
    with connect(lake, [manifest]) as con:
        row = con.execute("SELECT count(*) FROM bars").fetchone()
    assert row is not None
    return int(row[0])


def test_query_reads_exactly_the_pinned_version(lake: Lake) -> None:
    _ingest(lake)
    old = lake.head(DatasetKind.BARS)
    _ingest(lake, **LATER)
    new = lake.head(DatasetKind.BARS)
    assert old is not None
    assert new is not None
    assert _count(lake, old) == old.row_count
    assert _count(lake, new) == new.row_count > old.row_count
    with connect(lake, [new]) as con:
        symbols = con.execute("SELECT DISTINCT symbol FROM bars ORDER BY 1").fetchall()
    assert symbols == [("AAA",), ("BBB",)]


def test_query_refuses_tampered_version(lake: Lake) -> None:
    _ingest(lake)
    manifest = lake.head(DatasetKind.BARS)
    assert manifest is not None
    first = next(iter(manifest.partitions.values()))
    lake.resolve(first.path).write_bytes(b"tampered")
    with pytest.raises(LakeIntegrityError):
        connect(lake, [manifest])


def test_rebuild_reproduces_incrementally_built_version(lake: Lake, tmp_path: Path) -> None:
    _ingest(lake)
    _ingest(lake, **LATER)
    manifest = lake.head(DatasetKind.BARS)
    assert manifest is not None
    result = rebuild(lake, manifest, tmp_path / "scratch", now=NOW)
    assert result.reproduced
    assert result.rows == manifest.row_count


def test_rebuild_fails_when_raw_payload_is_missing(lake: Lake, tmp_path: Path) -> None:
    _ingest(lake)
    manifest = lake.head(DatasetKind.BARS)
    assert manifest is not None
    next((lake.root / "raw").rglob("*.meta.json")).unlink()
    with pytest.raises(RebuildError, match="missing"):
        rebuild(lake, manifest, tmp_path / "scratch", now=NOW)


def test_rebuild_detects_corrupted_raw_bytes(lake: Lake, tmp_path: Path) -> None:
    _ingest(lake)
    manifest = lake.head(DatasetKind.BARS)
    assert manifest is not None
    next((lake.root / "raw").rglob("*.csv")).write_bytes(b"corrupted")
    with pytest.raises(LakeIntegrityError):
        rebuild(lake, manifest, tmp_path / "scratch", now=NOW)
