"""credentials for apps connected from the app

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-22 09:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "app_connections",
        sa.Column("app_id", sa.String(length=32), nullable=False),
        sa.Column("data_enc", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("app_id"),
    )


def downgrade() -> None:
    op.drop_table("app_connections")
