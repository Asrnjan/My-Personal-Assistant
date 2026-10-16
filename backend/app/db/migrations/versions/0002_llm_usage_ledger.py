"""llm usage ledger

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-16 08:47:57.171564
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "llm_usage",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("model", sa.String(length=64), nullable=False),
        sa.Column("purpose", sa.String(length=64), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("cache_creation_input_tokens", sa.Integer(), nullable=False),
        sa.Column("cache_read_input_tokens", sa.Integer(), nullable=False),
        sa.Column("cost_usd", sa.Float(), nullable=False),
        sa.Column("request_id", sa.String(length=64), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("llm_usage", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_llm_usage_at"), ["at"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("llm_usage", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_llm_usage_at"))

    op.drop_table("llm_usage")
