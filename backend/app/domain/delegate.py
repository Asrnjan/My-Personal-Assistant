"""People the CEO can delegate work to, loaded from ``contacts.yaml``."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.domain.enums import Channel


class DelegateBase(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    full_name: str = Field(min_length=1)
    aliases: list[str] = Field(default_factory=list)
    email: EmailStr
    telegram_chat_id: str | None = None
    slack_user_id: str | None = None
    teams_webhook: str | None = None
    preferred_channels: list[Channel] = Field(default_factory=lambda: [Channel.EMAIL])
    ownership_areas: list[str] = Field(default_factory=list)
    jira_account_id: str | None = None
    asana_gid: str | None = None
    clickup_id: str | None = None


class Delegate(DelegateBase):
    id: str = Field(min_length=1)
