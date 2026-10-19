"""pending actions that aren't about an item (emails, Teams messages, meetings)

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-19 15:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD = ("delegate", "decline_meeting", "defer", "close_task", "drop")
_NEW = (*_OLD, "send_email", "reply_email", "teams_message", "book_meeting")


def upgrade() -> None:
    with op.batch_alter_table("pending_actions", schema=None) as batch_op:
        batch_op.alter_column("item_id", existing_type=sa.String(length=32), nullable=True)
        batch_op.alter_column(
            "kind",
            existing_type=sa.Enum(*_OLD, name="action_kind", native_enum=False),
            type_=sa.Enum(*_NEW, name="action_kind", native_enum=False),
            existing_nullable=False,
        )


def downgrade() -> None:
    op.execute("DELETE FROM pending_actions WHERE item_id IS NULL")
    with op.batch_alter_table("pending_actions", schema=None) as batch_op:
        batch_op.alter_column(
            "kind",
            existing_type=sa.Enum(*_NEW, name="action_kind", native_enum=False),
            type_=sa.Enum(*_OLD, name="action_kind", native_enum=False),
            existing_nullable=False,
        )
        batch_op.alter_column("item_id", existing_type=sa.String(length=32), nullable=False)
