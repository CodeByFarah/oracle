"""Research ledger: hypothesis tests, hypotheses, research journal.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-05
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ARRAY, JSONB

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NOW = sa.func.now()
_SEARCH_TEXT = (
    "question || ' ' || hypothesis || ' ' || method || ' ' || result || ' ' || "
    "interpretation || ' ' || failures || ' ' || next_experiment"
)


def upgrade() -> None:
    op.create_table(
        "hypothesis_tests",
        sa.Column("test_id", sa.Text(), primary_key=True),
        sa.Column("protocol_hash", sa.Text(), nullable=False, index=True),
        sa.Column("dataset_version", sa.Text(), nullable=False),
        sa.Column("feature_set_id", sa.Text(), nullable=False),
        sa.Column("split", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("candidate_key", sa.Text(), nullable=False),
        sa.Column("candidate", JSONB(), nullable=False),
        sa.Column("horizon", sa.Integer(), nullable=False),
        sa.Column("n_events", sa.Integer(), nullable=False),
        sa.Column("effect", sa.Float()),
        sa.Column("t_stat", sa.Float()),
        sa.Column("p_value", sa.Float()),
        sa.Column("p_adjusted", sa.Float()),
        sa.Column("cost_adjusted_effect_bps", sa.Float()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=_NOW),
        sa.CheckConstraint("split IN ('discovery','validation','holdout')", name="ck_tests_split"),
    )
    op.execute("CREATE SEQUENCE hypothesis_seq START 1")
    op.create_table(
        "hypotheses",
        sa.Column(
            "hypothesis_id",
            sa.Text(),
            primary_key=True,
            server_default=sa.text("'H-' || lpad(nextval('hypothesis_seq')::text, 7, '0')"),
        ),
        sa.Column("protocol_hash", sa.Text(), nullable=False, index=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("statement", sa.Text(), nullable=False),
        sa.Column("document", JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=_NOW),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=_NOW),
    )
    op.create_table(
        "journal_entries",
        sa.Column("entry_id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=_NOW),
        sa.Column("author", sa.Text(), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("hypothesis", sa.Text(), nullable=False),
        sa.Column("method", sa.Text(), nullable=False),
        sa.Column("data", sa.Text(), nullable=False),
        sa.Column("result", sa.Text(), nullable=False),
        sa.Column("interpretation", sa.Text(), nullable=False),
        sa.Column("failures", sa.Text(), nullable=False),
        sa.Column("next_experiment", sa.Text(), nullable=False),
        sa.Column("experiment_id", sa.Text()),
        sa.Column("hypothesis_id", sa.Text()),
        sa.Column("tags", ARRAY(sa.Text()), nullable=False, server_default="{}"),
    )
    op.execute(
        "CREATE INDEX ix_journal_search ON journal_entries "
        f"USING GIN (to_tsvector('english', {_SEARCH_TEXT}))"
    )


def downgrade() -> None:
    op.drop_table("journal_entries")
    op.drop_table("hypotheses")
    op.execute("DROP SEQUENCE IF EXISTS hypothesis_seq")
    op.drop_table("hypothesis_tests")
