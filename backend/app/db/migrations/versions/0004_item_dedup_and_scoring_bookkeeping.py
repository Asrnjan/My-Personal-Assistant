"""item dedup and scoring bookkeeping

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-16 09:07:56.659252
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("items", schema=None) as batch_op:
        batch_op.add_column(sa.Column("dedup_key", sa.String(length=512), nullable=True))
        batch_op.add_column(sa.Column("content_hash", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("scored_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.create_index(batch_op.f("ix_items_dedup_key"), ["dedup_key"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("items", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_items_dedup_key"))
        batch_op.drop_column("scored_at")
        batch_op.drop_column("content_hash")
        batch_op.drop_column("dedup_key")
