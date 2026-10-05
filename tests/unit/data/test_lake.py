from __future__ import annotations

import dataclasses
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from oracle.data.lake import Lake, LakeIntegrityError, split_partitions
from oracle.data.providers.base import RawPayload
from oracle.schemas.conform import DuplicateKeyConflictError, conform
from oracle.schemas.market import BARS, DatasetKind
from tests.unit.data.helpers import NOW, synthetic_table


def _payload(content: bytes = b"a,b\n1,2\n") -> RawPayload:
    return RawPayload(
        provider="synthetic",
        dataset=DatasetKind.BARS,
        content=content,
        content_type="text/csv",
        retrieved_at=NOW,
        request=(("symbol", "AAA"),),
    )


def test_put_raw_is_content_addressed_and_idempotent(lake: Lake) -> None:
    first = lake.put_raw(_payload())
    second = lake.put_raw(_payload())
    assert first == second
    assert first.path.endswith(f"{first.sha256}.csv")
    assert lake.read_raw(first) == b"a,b\n1,2\n"
    meta = json.loads((lake.root / first.path.replace(".csv", ".meta.json")).read_text())
    assert meta["request"] == {"symbol": "AAA"}
    assert meta["sha256"] == first.sha256


def test_put_raw_detects_corruption(lake: Lake) -> None:
    ref = lake.put_raw(_payload())
    (lake.root / ref.path).write_bytes(b"tampered")
    with pytest.raises(LakeIntegrityError, match="corrupted"):
        lake.put_raw(_payload())
    with pytest.raises(LakeIntegrityError, match="mismatch"):
        lake.read_raw(ref)


def test_unknown_content_type_gets_bin_extension(lake: Lake) -> None:
    payload = dataclasses.replace(_payload(), content_type="application/octet-stream")
    assert lake.put_raw(payload).path.endswith(".bin")


def test_commit_and_read_round_trip(lake: Lake) -> None:
    table = conform(synthetic_table(), BARS).table
    manifest = lake.commit(BARS, table, sources=["s1"], created_at=NOW)
    assert lake.head(DatasetKind.BARS) == manifest
    assert manifest.row_count == table.num_rows == 2 * 2 * 390
    assert manifest.parent_version is None
    assert conform(lake.read(manifest), BARS).table.equals(table)
    assert lake.verify(manifest) == []


def test_identical_commit_does_not_create_a_new_version(lake: Lake) -> None:
    table = synthetic_table()
    first = lake.commit(BARS, table, sources=["s1"], created_at=NOW)
    later = datetime(2027, 1, 1, tzinfo=UTC)
    assert lake.commit(BARS, table, sources=["s1"], created_at=later) == first
    assert len(list((lake.root / "manifests" / "bars").glob("ds-*.json"))) == 1


def test_new_rows_create_child_version_and_old_version_stays_reproducible(lake: Lake) -> None:
    old = lake.commit(BARS, synthetic_table(), sources=["s1"], created_at=NOW)
    extra = synthetic_table(
        start=datetime(2024, 1, 4, tzinfo=UTC), end=datetime(2024, 1, 5, tzinfo=UTC)
    )
    new = lake.commit(BARS, extra, sources=["s2"], created_at=NOW)

    assert new.parent_version == old.version
    assert new.row_count == old.row_count + extra.num_rows
    assert new.sources == ("s1", "s2")
    reloaded_old = lake.load_manifest(DatasetKind.BARS, old.version)
    assert lake.verify(reloaded_old) == []
    assert lake.read(reloaded_old).num_rows == old.row_count


def test_conflicting_revision_is_refused_and_head_unchanged(lake: Lake) -> None:
    original = lake.commit(BARS, synthetic_table(seed=1), sources=["s1"], created_at=NOW)
    with pytest.raises(DuplicateKeyConflictError):
        lake.commit(BARS, synthetic_table(seed=2), sources=["s2"], created_at=NOW)
    assert lake.head(DatasetKind.BARS) == original


def test_empty_commit_rejected(lake: Lake) -> None:
    with pytest.raises(ValueError, match="empty"):
        lake.commit(BARS, BARS.schema.empty_table(), sources=[], created_at=NOW)


def test_verify_reports_tampered_and_missing_files(lake: Lake) -> None:
    manifest = lake.commit(BARS, synthetic_table(), sources=["s1"], created_at=NOW)
    entries = sorted(manifest.partitions.values(), key=lambda e: e.path)
    (lake.root / entries[0].path).write_bytes(b"not parquet")
    (lake.root / entries[1].path).unlink()
    problems = lake.verify(manifest)
    assert any("hash mismatch" in p for p in problems)
    assert any("missing file" in p for p in problems)
    with pytest.raises(LakeIntegrityError):
        lake.read(manifest)


def test_tampered_manifest_is_rejected(lake: Lake) -> None:
    manifest = lake.commit(BARS, synthetic_table(), sources=["s1"], created_at=NOW)
    path = lake.root / "manifests" / "bars" / f"{manifest.version}.json"
    body = json.loads(path.read_text())
    first_key = next(iter(body["partitions"]))
    body["partitions"][first_key]["sha256"] = "0" * 64
    path.write_text(json.dumps(body))
    with pytest.raises(LakeIntegrityError, match="does not match"):
        lake.load_manifest(DatasetKind.BARS, manifest.version)


def test_schema_version_mismatch_requires_migration(lake: Lake) -> None:
    lake.commit(BARS, synthetic_table(), sources=["s1"], created_at=NOW)
    bars_v2 = dataclasses.replace(BARS, schema_version=2)
    with pytest.raises(LakeIntegrityError, match="migration"):
        lake.commit(bars_v2, synthetic_table(), sources=["s1"], created_at=NOW)


def test_same_input_gives_same_version_in_independent_lakes(tmp_path: Path) -> None:
    versions = {
        Lake(tmp_path / name).commit(BARS, synthetic_table(), sources=["s"], created_at=NOW).version
        for name in ("a", "b")
    }
    assert len(versions) == 1


def test_partition_layout_and_path_quoting() -> None:
    table = conform(synthetic_table(symbols=("BRK/B",), interval="1d"), BARS).table
    parts = split_partitions(table, BARS)
    assert [key for key, _ in parts] == [
        "source=synthetic/interval=1d/symbol=BRK%2FB/event_ts_month=2024-01"
    ]
    assert sum(t.num_rows for _, t in parts) == table.num_rows


def test_month_partitioning_uses_utc() -> None:
    table = conform(
        synthetic_table(
            symbols=("AAA",),
            interval="1d",
            start=datetime(2024, 1, 31, tzinfo=UTC),
            end=datetime(2024, 2, 2, tzinfo=UTC),
        ),
        BARS,
    ).table
    months = [key.rsplit("=", 1)[1] for key, _ in split_partitions(table, BARS)]
    assert months == ["2024-01", "2024-02"]
