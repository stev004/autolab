"""Persist the reason an optimizer run is paused.

Revision ID: 0003
Revises: 0002
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | Sequence[str] | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("optimizer_runs", schema=None) as batch_op:
        batch_op.add_column(sa.Column("status_reason", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("optimizer_runs", schema=None) as batch_op:
        batch_op.drop_column("status_reason")
