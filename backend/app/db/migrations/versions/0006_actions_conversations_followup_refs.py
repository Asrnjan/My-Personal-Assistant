"""pending actions, conversations, follow-up refs

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-16 10:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_KINDS = ("delegate", "decline_meeting", "defer", "close_task", "drop")
_STATUSES = ("pending", "executed", "failed", "cancelled")


def upgrade() -> None:
    op.create_table(
        "pending_actions",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("conversation_id", sa.String(length=64), nullable=True),
        sa.Column("kind", sa.Enum(*_KINDS, name="action_kind", native_enum=False), nullable=False),
        sa.Column("item_id", sa.String(length=32), nullable=False),
        sa.Column("params", sa.JSON(), nullable=False),
        sa.Column("readback", sa.Text(), nullable=False),
        sa.Column(
            "status", sa.Enum(*_STATUSES, name="action_status", native_enum=False), nullable=False
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decision_id", sa.String(length=32), nullable=True),
        sa.Column("result", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["decision_id"], ["decisions.id"]),
        sa.ForeignKeyConstraint(["item_id"], ["items.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("pending_actions", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_pending_actions_conversation_id"), ["conversation_id"], unique=False
        )
        batch_op.create_index(batch_op.f("ix_pending_actions_status"), ["status"], unique=False)
    op.create_table(
        "conversations",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("history", sa.JSON(), nullable=False),
        sa.Column("last_listed", sa.JSON(), nullable=False),
        sa.Column("awaiting", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("followups", schema=None) as batch_op:
        batch_op.add_column(sa.Column("refs", sa.JSON(), nullable=False, server_default="{}"))
        batch_op.add_column(
            sa.Column("seen_replies", sa.JSON(), nullable=False, server_default="[]")
        )
        batch_op.add_column(sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True))
    with op.batch_alter_table("items", schema=None) as batch_op:
        batch_op.add_column(sa.Column("deferred_until", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("items", schema=None) as batch_op:
        batch_op.drop_column("deferred_until")
    with op.batch_alter_table("followups", schema=None) as batch_op:
        batch_op.drop_column("sent_at")
        batch_op.drop_column("seen_replies")
        batch_op.drop_column("refs")
    op.drop_table("conversations")
    with op.batch_alter_table("pending_actions", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_pending_actions_status"))
        batch_op.drop_index(batch_op.f("ix_pending_actions_conversation_id"))
    op.drop_table("pending_actions")
