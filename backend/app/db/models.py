"""SQLAlchemy ORM models. The schema is managed by Alembic migrations."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any, ClassVar

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    event,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Mapper, mapped_column, relationship

from app.domain.enums import (
    Actor,
    ApprovedVia,
    DecisionAction,
    FollowUpState,
    ItemKind,
    ItemStatus,
    PriorityBand,
    SignalKind,
    Source,
)


def utcnow() -> datetime:
    return datetime.now(UTC)


def new_id() -> str:
    return uuid.uuid4().hex


def _enum(cls: type[Any], name: str) -> Enum:
    return Enum(cls, name=name, native_enum=False, values_callable=lambda e: [m.value for m in e])


class Base(DeclarativeBase):
    type_annotation_map: ClassVar[dict[Any, Any]] = {datetime: DateTime(timezone=True)}


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class ItemRow(TimestampMixin, Base):
    __tablename__ = "items"
    __table_args__ = (UniqueConstraint("source", "source_id", name="uq_items_source"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    source: Mapped[Source] = mapped_column(_enum(Source, "item_source"))
    source_id: Mapped[str] = mapped_column(String(255))
    kind: Mapped[ItemKind] = mapped_column(_enum(ItemKind, "item_kind"))
    title: Mapped[str] = mapped_column(String(500))
    description: Mapped[str] = mapped_column(Text, default="")
    url: Mapped[str | None] = mapped_column(String(2000))
    requester: Mapped[str | None] = mapped_column(String(255))
    participants: Mapped[list[str]] = mapped_column(JSON, default=list)
    start: Mapped[datetime | None]
    end: Mapped[datetime | None]
    due: Mapped[datetime | None]
    estimated_minutes: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[ItemStatus] = mapped_column(
        _enum(ItemStatus, "item_status"), default=ItemStatus.OPEN
    )
    priority_score: Mapped[int | None] = mapped_column(Integer)
    priority_band: Mapped[PriorityBand | None] = mapped_column(_enum(PriorityBand, "priority_band"))
    reason: Mapped[str | None] = mapped_column(String(300))
    confidence: Mapped[float | None] = mapped_column(Float)
    ceo_required: Mapped[bool | None] = mapped_column(Boolean)
    suggested_delegate_id: Mapped[str | None] = mapped_column(ForeignKey("delegates.id"))
    last_synced_at: Mapped[datetime | None]

    decisions: Mapped[list[DecisionRow]] = relationship(back_populates="item")


class DelegateRow(TimestampMixin, Base):
    __tablename__ = "delegates"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    full_name: Mapped[str] = mapped_column(String(255))
    aliases: Mapped[list[str]] = mapped_column(JSON, default=list)
    email: Mapped[str] = mapped_column(String(320))
    telegram_chat_id: Mapped[str | None] = mapped_column(String(64))
    slack_user_id: Mapped[str | None] = mapped_column(String(64))
    teams_webhook: Mapped[str | None] = mapped_column(String(2000))
    preferred_channels: Mapped[list[str]] = mapped_column(JSON, default=list)
    ownership_areas: Mapped[list[str]] = mapped_column(JSON, default=list)
    jira_account_id: Mapped[str | None] = mapped_column(String(128))
    asana_gid: Mapped[str | None] = mapped_column(String(64))
    clickup_id: Mapped[str | None] = mapped_column(String(64))


class DecisionRow(Base):
    __tablename__ = "decisions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    item_id: Mapped[str] = mapped_column(ForeignKey("items.id"))
    action: Mapped[DecisionAction] = mapped_column(_enum(DecisionAction, "decision_action"))
    actor: Mapped[Actor] = mapped_column(_enum(Actor, "decision_actor"))
    delegate_id: Mapped[str | None] = mapped_column(ForeignKey("delegates.id"))
    brief_text: Mapped[str | None] = mapped_column(Text)
    channels_used: Mapped[list[str]] = mapped_column(JSON, default=list)
    approved_via: Mapped[ApprovedVia] = mapped_column(_enum(ApprovedVia, "approved_via"))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    executed_at: Mapped[datetime | None]
    result: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)

    item: Mapped[ItemRow] = relationship(back_populates="decisions")
    followups: Mapped[list[FollowUpRow]] = relationship(back_populates="decision")


class FollowUpRow(TimestampMixin, Base):
    __tablename__ = "followups"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    decision_id: Mapped[str] = mapped_column(ForeignKey("decisions.id"))
    due: Mapped[datetime | None]
    acknowledged_at: Mapped[datetime | None]
    last_nudge_at: Mapped[datetime | None]
    nudges_sent: Mapped[int] = mapped_column(Integer, default=0)
    state: Mapped[FollowUpState] = mapped_column(
        _enum(FollowUpState, "followup_state"), default=FollowUpState.PENDING
    )

    decision: Mapped[DecisionRow] = relationship(back_populates="followups")


class LearningSignalRow(Base):
    """A CEO override or explicit preference, folded back into scoring prompts."""

    __tablename__ = "learning_signals"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    kind: Mapped[SignalKind] = mapped_column(_enum(SignalKind, "signal_kind"))
    item_id: Mapped[str | None] = mapped_column(ForeignKey("items.id"))
    rule_text: Mapped[str] = mapped_column(Text)
    data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class AuditLogRow(Base):
    """Immutable record of every external action and its outcome."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    at: Mapped[datetime] = mapped_column(default=utcnow)
    actor: Mapped[str] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(128))
    target: Mapped[str | None] = mapped_column(String(255))
    decision_id: Mapped[str | None] = mapped_column(ForeignKey("decisions.id"))
    outcome: Mapped[str] = mapped_column(String(32))
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class ImmutableRecordError(RuntimeError):
    pass


@event.listens_for(AuditLogRow, "before_update")
@event.listens_for(AuditLogRow, "before_delete")
def _forbid_audit_mutation(_mapper: Mapper[Any], _conn: Any, _target: AuditLogRow) -> None:
    raise ImmutableRecordError("audit log entries are immutable")


class ApiTokenRow(Base):
    """A bearer token issued to a client (desktop, PWA, Telegram). Only the hash is stored."""

    __tablename__ = "api_tokens"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    client_name: Mapped[str] = mapped_column(String(64))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    revoked_at: Mapped[datetime | None]
