"""Record dataset versions (lake manifests) in the metadata database."""

from __future__ import annotations

from sqlalchemy import Engine, select
from sqlalchemy.dialects.postgresql import insert

from oracle.data.manifest import DatasetManifest
from oracle.registry.models import AuditLog, DatasetVersion


def register_manifest(engine: Engine, manifest: DatasetManifest, *, actor: str) -> bool:
    """Insert the version if new. Returns True when a row was inserted.

    A version id is a content hash, so re-registering the same version is a no-op.
    """
    with engine.begin() as conn:
        inserted = conn.execute(
            insert(DatasetVersion)
            .values(
                version=manifest.version,
                dataset=manifest.dataset.value,
                schema_version=manifest.schema_version,
                parent_version=manifest.parent_version,
                row_count=manifest.row_count,
                file_count=len(manifest.partitions),
                manifest=manifest.model_dump(mode="json"),
                created_at=manifest.created_at,
            )
            .on_conflict_do_nothing(index_elements=["version"])
            .returning(DatasetVersion.version)
        ).first()
        if inserted is not None:
            conn.execute(
                insert(AuditLog).values(
                    actor=actor,
                    action="register_dataset_version",
                    subject_type="dataset_version",
                    subject_id=manifest.version,
                    details={"dataset": manifest.dataset.value, "rows": manifest.row_count},
                )
            )
    return inserted is not None


def registered_versions(engine: Engine, dataset: str) -> list[str]:
    with engine.connect() as conn:
        rows = conn.execute(
            select(DatasetVersion.version)
            .where(DatasetVersion.dataset == dataset)
            .order_by(DatasetVersion.registered_at)
        )
        return [r[0] for r in rows]
