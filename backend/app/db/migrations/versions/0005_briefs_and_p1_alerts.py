"""briefs and p1 alerts

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-16 09:45:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "briefs",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("for_date", sa.Date(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("briefs", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_briefs_kind"), ["kind"], unique=False)
        batch_op.create_index(batch_op.f("ix_briefs_created_at"), ["created_at"], unique=False)
    with op.batch_alter_table("items", schema=None) as batch_op:
        batch_op.add_column(sa.Column("alerted_hash", sa.String(length=64), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("items", schema=None) as batch_op:
        batch_op.drop_column("alerted_hash")
    with op.batch_alter_table("briefs", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_briefs_created_at"))
        batch_op.drop_index(batch_op.f("ix_briefs_kind"))
    op.drop_table("briefs")
