"""Add immutable external measurement plans, batches, and revisions.

Revision ID: 0005
Revises: 0004
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | Sequence[str] | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "expected_measurement_plans",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("experiment_id", sa.String(length=36), nullable=False),
        sa.Column("protocol_id", sa.String(length=36), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("schema_version", sa.String(length=32), nullable=False),
        sa.Column("protocol_hash", sa.String(length=64), nullable=False),
        sa.Column("plan_sha256", sa.String(length=64), nullable=False),
        sa.Column("plan_snapshot", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["experiment_id"], ["experiments.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["protocol_id"], ["protocols.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("experiment_id", "version", name="uq_measurement_plan_version"),
        sa.UniqueConstraint("plan_sha256"),
    )
    op.create_index(
        op.f("ix_expected_measurement_plans_experiment_id"),
        "expected_measurement_plans",
        ["experiment_id"],
        unique=False,
    )
    op.create_table(
        "measurement_ingestion_batches",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("experiment_id", sa.String(length=36), nullable=False),
        sa.Column("plan_id", sa.String(length=36), nullable=False),
        sa.Column("source_type", sa.String(length=32), nullable=False),
        sa.Column("batch_kind", sa.String(length=32), nullable=False),
        sa.Column("submitted_by", sa.String(length=128), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source_file_sha256", sa.String(length=64), nullable=True),
        sa.Column("source_filename", sa.String(length=512), nullable=True),
        sa.Column("batch_sha256", sa.String(length=64), nullable=False),
        sa.Column("batch_snapshot", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["experiment_id"], ["experiments.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["plan_id"], ["expected_measurement_plans.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("batch_sha256"),
        sa.UniqueConstraint(
            "experiment_id", "source_file_sha256", name="uq_measurement_source_file"
        ),
    )
    op.create_index(
        "ix_measurement_batches_experiment_source",
        "measurement_ingestion_batches",
        ["experiment_id", "source_type"],
        unique=False,
    )
    op.create_table(
        "external_measurement_revisions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("experiment_id", sa.String(length=36), nullable=False),
        sa.Column("plan_id", sa.String(length=36), nullable=False),
        sa.Column("batch_id", sa.String(length=36), nullable=False),
        sa.Column("expected_measurement_id", sa.String(length=255), nullable=False),
        sa.Column("revision_number", sa.Integer(), nullable=False),
        sa.Column("supersedes_revision_id", sa.String(length=36), nullable=True),
        sa.Column("source_type", sa.String(length=32), nullable=False),
        sa.Column("availability", sa.String(length=32), nullable=False),
        sa.Column("value", sa.Float(), nullable=True),
        sa.Column("value_decimal", sa.String(length=255), nullable=True),
        sa.Column("unit", sa.String(length=128), nullable=False),
        sa.Column("measured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("correction_reason", sa.Text(), nullable=True),
        sa.Column("revision_sha256", sa.String(length=64), nullable=False),
        sa.Column("raw_snapshot", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["batch_id"], ["measurement_ingestion_batches.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["experiment_id"], ["experiments.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["plan_id"], ["expected_measurement_plans.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["supersedes_revision_id"],
            ["external_measurement_revisions.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "plan_id",
            "expected_measurement_id",
            "revision_number",
            name="uq_external_measurement_revision",
        ),
        sa.UniqueConstraint("revision_sha256"),
        sa.UniqueConstraint("supersedes_revision_id"),
    )
    op.create_index(
        "ix_external_revision_experiment_expected",
        "external_measurement_revisions",
        ["experiment_id", "expected_measurement_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_external_revision_experiment_expected",
        table_name="external_measurement_revisions",
    )
    op.drop_table("external_measurement_revisions")
    op.drop_index(
        "ix_measurement_batches_experiment_source",
        table_name="measurement_ingestion_batches",
    )
    op.drop_table("measurement_ingestion_batches")
    op.drop_index(
        op.f("ix_expected_measurement_plans_experiment_id"),
        table_name="expected_measurement_plans",
    )
    op.drop_table("expected_measurement_plans")
