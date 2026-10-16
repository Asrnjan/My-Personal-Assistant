"""Writing to the immutable audit log."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.db.models import AuditLogRow


def audit(
    session: Session,
    action: str,
    outcome: str,
    *,
    target: str | None = None,
    decision_id: str | None = None,
    actor: str = "assistant",
    **details: Any,
) -> None:
    """Record one external action and how it went (``ok``, ``failed``, ``skipped``...)."""
    session.add(
        AuditLogRow(
            actor=actor,
            action=action,
            target=target,
            decision_id=decision_id,
            outcome=outcome,
            details=details,
        )
    )
