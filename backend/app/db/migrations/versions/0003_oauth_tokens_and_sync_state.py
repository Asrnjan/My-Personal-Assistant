"""oauth tokens and sync state

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-16 09:01:07.225697
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "oauth_states",
        sa.Column("state", sa.String(length=64), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("code_verifier", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("state"),
    )
    op.create_table(
        "oauth_tokens",
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("access_token_enc", sa.Text(), nullable=False),
        sa.Column("refresh_token_enc", sa.Text(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("scopes", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("provider"),
    )
    op.create_table(
        "sync_state",
        sa.Column("adapter", sa.String(length=32), nullable=False),
        sa.Column("feed", sa.String(length=64), nullable=False),
        sa.Column("cursor", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("adapter", "feed"),
    )


def downgrade() -> None:
    op.drop_table("sync_state")
    op.drop_table("oauth_tokens")
    op.drop_table("oauth_states")
