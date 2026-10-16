"""SQLAlchemy ORM models. The schema is managed by Alembic migrations."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from typing import Any, ClassVar

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    TypeDecorator,
    UniqueConstraint,
    event,
)
from sqlalchemy.engine import Dialect
from sqlalchemy.orm import DeclarativeBase, Mapped, Mapper, mapped_column, relationship

from app.domain.enums import (
    ActionKind,
    ActionStatus,
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


class UTCDateTime(TypeDecorator[datetime]):
    """Stores datetimes as UTC and always returns them timezone-aware.

    SQLite drops timezone information, so values are normalised to UTC on the
    way in and tagged as UTC on the way out. Naive datetimes are rejected.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetimes are not allowed; use timezone-aware values")
        return value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class Base(DeclarativeBase):
    type_annotation_map: ClassVar[dict[Any, Any]] = {datetime: UTCDateTime()}


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
    # Same real-world meeting across sources (e.g. "ical:<uid>"); used for dedup.
    dedup_key: Mapped[str | None] = mapped_column(String(512), index=True)
    # Hash of the content the priority engine reads; a change means re-score.
    content_hash: Mapped[str | None] = mapped_column(String(64))
    scored_at: Mapped[datetime | None]
    # Fingerprint of this item when the CEO last heard about it as a P1 (alert or brief).
    alerted_hash: Mapped[str | None] = mapped_column(String(64))
    # A deferred item reopens once this passes.
    deferred_until: Mapped[datetime | None]

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
    # Where the delegation went, per channel: {"email": {"thread_id": ...}, "telegram": {...}}.
    refs: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # Provider ids of replies already classified, so each is read once.
    seen_replies: Mapped[list[str]] = mapped_column(JSON, default=list)
    sent_at: Mapped[datetime | None]

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


class LLMUsageRow(Base):
    """Token usage and estimated cost of one Claude API call."""

    __tablename__ = "llm_usage"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    at: Mapped[datetime] = mapped_column(default=utcnow, index=True)
    model: Mapped[str] = mapped_column(String(64))
    purpose: Mapped[str] = mapped_column(String(64))
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_creation_input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_read_input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    request_id: Mapped[str | None] = mapped_column(String(64))


class OAuthTokenRow(TimestampMixin, Base):
    """OAuth tokens for one provider, encrypted at rest with Fernet."""

    __tablename__ = "oauth_tokens"

    provider: Mapped[str] = mapped_column(String(32), primary_key=True)
    access_token_enc: Mapped[str] = mapped_column(Text)
    refresh_token_enc: Mapped[str | None] = mapped_column(Text)
    expires_at: Mapped[datetime | None]
    scopes: Mapped[str] = mapped_column(Text, default="")


class OAuthStateRow(Base):
    """One-time ``state`` values for in-flight OAuth authorizations (CSRF protection)."""

    __tablename__ = "oauth_states"

    state: Mapped[str] = mapped_column(String(64), primary_key=True)
    provider: Mapped[str] = mapped_column(String(32))
    code_verifier: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class SyncStateRow(TimestampMixin, Base):
    """Incremental-sync cursors (sync tokens, delta links) per adapter and feed."""

    __tablename__ = "sync_state"

    adapter: Mapped[str] = mapped_column(String(32), primary_key=True)
    feed: Mapped[str] = mapped_column(String(64), primary_key=True)
    cursor: Mapped[str] = mapped_column(Text)


class BriefRow(Base):
    """A brief that was delivered (pushed) to the CEO: morning, evening or alert."""

    __tablename__ = "briefs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    kind: Mapped[str] = mapped_column(String(16), index=True)
    for_date: Mapped[date] = mapped_column(Date)
    text: Mapped[str] = mapped_column(Text)
    data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, index=True)


class PendingActionRow(Base):
    """An action proposed to the CEO, waiting for confirmation (or resolved)."""

    __tablename__ = "pending_actions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    conversation_id: Mapped[str | None] = mapped_column(String(64), index=True)
    kind: Mapped[ActionKind] = mapped_column(_enum(ActionKind, "action_kind"))
    item_id: Mapped[str] = mapped_column(ForeignKey("items.id"))
    params: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    readback: Mapped[str] = mapped_column(Text)
    status: Mapped[ActionStatus] = mapped_column(
        _enum(ActionStatus, "action_status"), default=ActionStatus.PENDING, index=True
    )
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    resolved_at: Mapped[datetime | None]
    decision_id: Mapped[str | None] = mapped_column(ForeignKey("decisions.id"))
    result: Mapped[str | None] = mapped_column(Text)


class ConversationRow(Base):
    """Dialogue state: recent turns, what "the second one" refers to, what awaits a yes."""

    __tablename__ = "conversations"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    history: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    last_listed: Mapped[list[str]] = mapped_column(JSON, default=list)
    awaiting: Mapped[list[str]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow)


class PushSubscriptionRow(Base):
    """A browser's Web Push subscription (from the PWA's ``PushManager``)."""

    __tablename__ = "push_subscriptions"

    endpoint: Mapped[str] = mapped_column(String(1024), primary_key=True)
    p256dh: Mapped[str] = mapped_column(String(256))
    auth: Mapped[str] = mapped_column(String(64))
    client_id: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
