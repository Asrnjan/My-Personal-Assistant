"""Builds the adapters enabled in ``config.yaml`` from the credentials in ``.env``."""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass, field

import httpx
from sqlalchemy.orm import Session, sessionmaker

from app.clock import Clock, utc_now
from app.config import AppConfig, Settings
from app.db.models import SyncStateRow
from app.integrations.base import CalendarAdapter, ChatAdapter, EmailAdapter, TaskAdapter
from app.integrations.google import calendar as gcal
from app.integrations.google import gmail
from app.integrations.http import HttpClient, Sleep
from app.integrations.microsoft import calendar as mscal
from app.integrations.microsoft import graph
from app.integrations.microsoft import mail as msmail
from app.integrations.microsoft import teams as msteams
from app.integrations.oauth import OAuthManager
from app.integrations.tasks import asana, clickup, jira

log = logging.getLogger(__name__)


@dataclass
class Adapters:
    calendars: list[CalendarAdapter] = field(default_factory=list)
    mail: list[EmailAdapter] = field(default_factory=list)
    tasks: list[TaskAdapter] = field(default_factory=list)
    # Teams chats, when the Teams account is connected.
    chat: ChatAdapter | None = None
    # "<adapter>: <what is missing>" for adapters that are enabled but not set up.
    not_configured: list[str] = field(default_factory=list)


def build_adapters(
    settings: Settings,
    config: AppConfig,
    oauth: OAuthManager | None,
    *,
    transport: httpx.BaseTransport | None = None,
    sleep: Sleep = time.sleep,
) -> Adapters:
    out = Adapters()

    def client(service: str, base_url: str, headers: Mapping[str, str]) -> HttpClient:
        return HttpClient(
            service, base_url, auth_headers=lambda: headers, transport=transport, sleep=sleep
        )

    def oauth_client(provider: str, base_url: str) -> HttpClient | None:
        if oauth is None:
            out.not_configured.append(f"{provider}: FERNET_KEY is not set")
            return None
        if not oauth.is_connected(provider):
            out.not_configured.append(f"{provider}: account not connected (Apps tab in the app)")
            return None
        return HttpClient(
            provider,
            base_url,
            auth_headers=lambda: {"Authorization": f"Bearer {oauth.access_token(provider)}"},
            on_unauthorized=lambda: oauth.refresh(provider),
            transport=transport,
            sleep=sleep,
        )

    enabled = config.adapters
    # An account connected from the app is used even when config.yaml doesn't list it.
    connected = {p for p in ("google", "microsoft") if oauth is not None and oauth.is_connected(p)}
    calendars = set(enabled.calendar) | connected
    email = set(enabled.email) | connected
    if "google" in calendars and (c := oauth_client("google", gcal.BASE_URL)):
        out.calendars.append(gcal.GoogleCalendarAdapter(c))
    if "google" in email and (c := oauth_client("google", gmail.BASE_URL)):
        out.mail.append(gmail.GmailAdapter(c))
    if "microsoft" in calendars and (c := oauth_client("microsoft", graph.BASE_URL)):
        out.calendars.append(mscal.OutlookCalendarAdapter(c))
    if "microsoft" in email and (c := oauth_client("microsoft", graph.BASE_URL)):
        out.mail.append(msmail.OutlookMailAdapter(c))
    teams_granted = "chat.readwrite" in (oauth.granted_scopes("microsoft") if oauth else set())
    if (
        "microsoft" in connected
        and teams_granted
        and (c := oauth_client("microsoft", graph.BASE_URL))
    ):
        out.chat = msteams.TeamsChatAdapter(c)

    # Credentials entered in the app switch an integration on without editing config.yaml.
    if "jira" in enabled.tasks or settings.jira_api_token.get_secret_value():
        token = settings.jira_api_token.get_secret_value()
        if settings.jira_base_url and settings.jira_email and token:
            headers = jira.basic_auth_header(settings.jira_email, token)
            out.tasks.append(
                jira.JiraAdapter(
                    client("jira", settings.jira_base_url, headers), settings.jira_base_url
                )
            )
        else:
            out.not_configured.append("jira: set JIRA_BASE_URL, JIRA_EMAIL and JIRA_API_TOKEN")
    if "asana" in enabled.tasks or settings.asana_access_token.get_secret_value():
        token = settings.asana_access_token.get_secret_value()
        if token and settings.asana_workspace_gid:
            headers = {"Authorization": f"Bearer {token}"}
            out.tasks.append(
                asana.AsanaAdapter(
                    client("asana", asana.BASE_URL, headers), settings.asana_workspace_gid
                )
            )
        else:
            out.not_configured.append("asana: set ASANA_ACCESS_TOKEN and ASANA_WORKSPACE_GID")
    if "clickup" in enabled.tasks or settings.clickup_api_token.get_secret_value():
        token = settings.clickup_api_token.get_secret_value()
        if token and settings.clickup_team_id and settings.clickup_user_id:
            headers = {"Authorization": token}
            out.tasks.append(
                clickup.ClickUpAdapter(
                    client("clickup", clickup.BASE_URL, headers),
                    settings.clickup_team_id,
                    settings.clickup_user_id,
                    settings.clickup_done_status,
                )
            )
        else:
            out.not_configured.append(
                "clickup: set CLICKUP_API_TOKEN, CLICKUP_TEAM_ID and CLICKUP_USER_ID"
            )

    for line in out.not_configured:
        log.warning("integration skipped - %s", line)
    return out


class SyncStateStore:
    """Persists incremental-sync cursors between runs."""

    def __init__(self, session_factory: sessionmaker[Session], clock: Clock = utc_now) -> None:
        self._sessions = session_factory
        self._clock = clock

    def get(self, adapter: str, feed: str) -> str | None:
        with self._sessions() as s:
            row = s.get(SyncStateRow, (adapter, feed))
            return row.cursor if row else None

    def set(self, adapter: str, feed: str, cursor: str | None) -> None:
        with self._sessions.begin() as s:
            row = s.get(SyncStateRow, (adapter, feed))
            if cursor is None:
                if row is not None:
                    s.delete(row)
                return
            if row is None:
                s.add(SyncStateRow(adapter=adapter, feed=feed, cursor=cursor))
            else:
                row.cursor = cursor
                row.updated_at = self._clock()


__all__ = ["Adapters", "SyncStateStore", "build_adapters"]
