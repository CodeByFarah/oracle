"""Metadata tables. Schema changes go through Alembic migrations, never `create_all`."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, Boolean, CheckConstraint, DateTime, Integer, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class AuditLog(Base):
    """Append-only. UPDATE/DELETE/TRUNCATE are rejected by a database trigger."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    actor: Mapped[str] = mapped_column(Text, nullable=False)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    subject_type: Mapped[str] = mapped_column(Text, nullable=False)
    subject_id: Mapped[str | None] = mapped_column(Text)
    details: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )


class Experiment(Base):
    __tablename__ = "experiments"
    __table_args__ = (
        CheckConstraint(
            "status IN ('planned','running','completed','failed','aborted')",
            name="ck_experiments_status",
        ),
    )

    experiment_id: Mapped[str] = mapped_column(Text, primary_key=True)
    hypothesis_id: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    dataset_version: Mapped[str] = mapped_column(Text, nullable=False)
    feature_version: Mapped[str] = mapped_column(Text, nullable=False)
    code_commit: Mapped[str] = mapped_column(Text, nullable=False)
    code_dirty: Mapped[bool] = mapped_column(Boolean, nullable=False)
    model_version: Mapped[str | None] = mapped_column(Text)
    protocol_hash: Mapped[str | None] = mapped_column(Text)
    universe_definition: Mapped[str] = mapped_column(Text, nullable=False)
    transaction_cost_assumptions: Mapped[str] = mapped_column(Text, nullable=False)
    provenance_fingerprint: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    parameters: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    metrics: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    results: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    conclusion: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DatasetVersion(Base):
    """One row per lake manifest version (content-addressed, immutable)."""

    __tablename__ = "dataset_versions"

    version: Mapped[str] = mapped_column(Text, primary_key=True)
    dataset: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    parent_version: Mapped[str | None] = mapped_column(Text)
    row_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    file_count: Mapped[int] = mapped_column(Integer, nullable=False)
    manifest: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    registered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
