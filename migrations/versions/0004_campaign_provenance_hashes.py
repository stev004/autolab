"""Separate canonical config and runtime source-tree provenance.

Revision ID: 0004
Revises: 0003
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | Sequence[str] | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("campaigns", schema=None) as batch_op:
        batch_op.add_column(sa.Column("config_source_sha256", sa.String(length=64)))
        batch_op.add_column(sa.Column("git_dirty", sa.Boolean()))
        batch_op.add_column(sa.Column("source_tree_diff_sha256", sa.String(length=64)))


def downgrade() -> None:
    with op.batch_alter_table("campaigns", schema=None) as batch_op:
        batch_op.drop_column("source_tree_diff_sha256")
        batch_op.drop_column("git_dirty")
        batch_op.drop_column("config_source_sha256")
