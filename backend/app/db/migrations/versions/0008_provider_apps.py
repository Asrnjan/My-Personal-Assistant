"""oauth app keys entered in the app

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-19 11:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "provider_apps",
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("client_id", sa.String(length=256), nullable=False),
        sa.Column("client_secret_enc", sa.Text(), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("provider"),
    )


def downgrade() -> None:
    op.drop_table("provider_apps")
