"""Initial metadata schema: append-only audit log and experiments.

Revision ID: 0001
Revises:
Create Date: 2026-10-05
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "audit_log",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "occurred_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("subject_type", sa.Text(), nullable=False),
        sa.Column("subject_id", sa.Text()),
        sa.Column("details", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
    )
    op.execute(
        """
        CREATE FUNCTION audit_log_reject_mutation() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'audit_log is append-only (% rejected)', TG_OP;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        "CREATE TRIGGER audit_log_no_update_delete BEFORE UPDATE OR DELETE ON audit_log "
        "FOR EACH ROW EXECUTE FUNCTION audit_log_reject_mutation();"
    )
    op.execute(
        "CREATE TRIGGER audit_log_no_truncate BEFORE TRUNCATE ON audit_log "
        "FOR EACH STATEMENT EXECUTE FUNCTION audit_log_reject_mutation();"
    )

    op.create_table(
        "experiments",
        sa.Column("experiment_id", sa.Text(), primary_key=True),
        sa.Column("hypothesis_id", sa.Text()),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("dataset_version", sa.Text(), nullable=False),
        sa.Column("feature_version", sa.Text(), nullable=False),
        sa.Column("code_commit", sa.Text(), nullable=False),
        sa.Column("code_dirty", sa.Boolean(), nullable=False),
        sa.Column("model_version", sa.Text()),
        sa.Column("protocol_hash", sa.Text()),
        sa.Column("universe_definition", sa.Text(), nullable=False),
        sa.Column("transaction_cost_assumptions", sa.Text(), nullable=False),
        sa.Column("provenance_fingerprint", sa.Text(), nullable=False),
        sa.Column("parameters", JSONB(), nullable=False),
        sa.Column("metrics", JSONB()),
        sa.Column("results", JSONB()),
        sa.Column("conclusion", sa.Text()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "status IN ('planned','running','completed','failed','aborted')",
            name="ck_experiments_status",
        ),
    )
    op.create_index(
        "ix_experiments_provenance_fingerprint", "experiments", ["provenance_fingerprint"]
    )


def downgrade() -> None:
    op.drop_index("ix_experiments_provenance_fingerprint", table_name="experiments")
    op.drop_table("experiments")
    op.execute("DROP TRIGGER IF EXISTS audit_log_no_truncate ON audit_log;")
    op.execute("DROP TRIGGER IF EXISTS audit_log_no_update_delete ON audit_log;")
    op.drop_table("audit_log")
    op.execute("DROP FUNCTION IF EXISTS audit_log_reject_mutation();")
