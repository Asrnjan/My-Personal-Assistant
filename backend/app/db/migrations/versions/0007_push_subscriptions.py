"""web push subscriptions

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-16 14:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "push_subscriptions",
        sa.Column("endpoint", sa.String(length=1024), nullable=False),
        sa.Column("p256dh", sa.String(length=256), nullable=False),
        sa.Column("auth", sa.String(length=64), nullable=False),
        sa.Column("client_id", sa.String(length=32), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("endpoint"),
    )


def downgrade() -> None:
    op.drop_table("push_subscriptions")
