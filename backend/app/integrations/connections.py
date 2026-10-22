"""Connecting apps from the app itself: Claude, Jira, Asana, ClickUp, Telegram, Slack, Teams.

Each app lists the few things a person has to paste. Everything else (workspace ids, user
ids, chat ids) is looked up with the credentials just entered, so setup is "paste a token,
tap Connect". Values are encrypted at rest and applied to the running settings straight
away, so nothing needs a restart or a ``.env`` edit.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx
from pydantic import SecretStr
from sqlalchemy import delete
from sqlalchemy.orm import Session, sessionmaker

from app.clock import Clock, utc_now
from app.config import Settings
from app.db.models import AppConnectionRow
from app.integrations.tasks.jira import basic_auth_header
from app.security import TokenCipher

log = logging.getLogger(__name__)

TIMEOUT = 15.0


class SetupError(Exception):
    """A check failed; the message is safe to show to the person connecting."""


@dataclass(frozen=True)
class Field:
    key: str
    label: str
    secret: bool = False
    optional: bool = False
    placeholder: str = ""
    help: str = ""


@dataclass(frozen=True)
class AppSpec:
    id: str
    name: str
    group: str  # "brain", "tasks" or "chat"
    description: str
    fields: tuple[Field, ...]
    # Where each field lives on ``Settings``.
    settings: dict[str, str]
    steps: tuple[str, ...] = ()
    link: str = ""
    link_label: str = ""


APPS: tuple[AppSpec, ...] = (
    AppSpec(
        "claude",
        "Claude",
        "brain",
        "The AI that prioritises your day and talks with you",
        (
            Field(
                "api_key",
                "API key",
                secret=True,
                placeholder="sk-ant-...",
                help="Starts with sk-ant-",
            ),
        ),
        {"api_key": "anthropic_api_key"},
        ("Open the Anthropic console and create an API key.", "Paste it here."),
        "https://console.anthropic.com/settings/keys",
        "Get a key",
    ),
    AppSpec(
        "jira",
        "Jira",
        "tasks",
        "Issues assigned to you",
        (
            Field(
                "base_url",
                "Your Jira address",
                placeholder="https://yourteam.atlassian.net",
            ),
            Field("email", "Your Atlassian email", placeholder="you@company.com"),
            Field("api_token", "API token", secret=True),
        ),
        {"base_url": "jira_base_url", "email": "jira_email", "api_token": "jira_api_token"},
        ("Open your Atlassian security page.", "Create an API token and paste it here."),
        "https://id.atlassian.com/manage-profile/security/api-tokens",
        "Create a token",
    ),
    AppSpec(
        "asana",
        "Asana",
        "tasks",
        "Tasks assigned to you",
        (Field("token", "Personal access token", secret=True),),
        {"token": "asana_access_token", "workspace": "asana_workspace_gid"},
        ("Open the Asana developer console.", "Create a personal access token and paste it."),
        "https://app.asana.com/0/my-apps",
        "Create a token",
    ),
    AppSpec(
        "clickup",
        "ClickUp",
        "tasks",
        "Tasks assigned to you",
        (Field("token", "Personal API token", secret=True, placeholder="pk_..."),),
        {
            "token": "clickup_api_token",
            "team": "clickup_team_id",
            "user": "clickup_user_id",
        },
        ("Open ClickUp, then Settings and Apps.", "Copy your personal API token and paste it."),
        "https://app.clickup.com/settings/apps",
        "Open ClickUp apps",
    ),
    AppSpec(
        "telegram",
        "Telegram",
        "chat",
        "Talk to your assistant and reach your team from Telegram",
        (
            Field(
                "bot_token",
                "Bot token",
                secret=True,
                placeholder="123456:ABC-...",
                help="From @BotFather",
            ),
            Field(
                "chat_id",
                "Your chat id",
                optional=True,
                help="Leave empty: send your bot a message, then tap Connect.",
            ),
        ),
        {"bot_token": "telegram_bot_token", "chat_id": "telegram_ceo_chat_id"},
        (
            "In Telegram, message @BotFather and send /newbot.",
            "Paste the token it gives you.",
            "Open your new bot, press Start, then tap Connect.",
        ),
        "https://t.me/BotFather",
        "Open BotFather",
    ),
    AppSpec(
        "slack",
        "Slack",
        "chat",
        "Message your team on Slack",
        (
            Field("bot_token", "Bot token", secret=True, placeholder="xoxb-..."),
            Field(
                "user_id",
                "Your member id",
                optional=True,
                help="Optional. Leave empty to find it from your email.",
            ),
        ),
        {"bot_token": "slack_bot_token", "user_id": "slack_ceo_user_id"},
        (
            "Create a Slack app and add the bot scopes chat:write, im:write, "
            "im:history and users:read.email.",
            "Install it to your workspace and paste the bot token.",
        ),
        "https://api.slack.com/apps",
        "Create a Slack app",
    ),
    AppSpec(
        "teams-webhook",
        "Teams channel",
        "chat",
        "Post notices to a Teams channel you read",
        (
            Field(
                "webhook_url",
                "Webhook URL",
                secret=True,
                placeholder="https://...webhook.office.com/...",
            ),
        ),
        {"webhook_url": "teams_webhook_url"},
        (
            "In Teams, open the channel menu and choose Workflows or Connectors.",
            "Add an incoming webhook and paste its URL.",
        ),
    ),
)
BY_ID = {a.id: a for a in APPS}


def _secret(spec: AppSpec, key: str) -> bool:
    return any(f.key == key and f.secret for f in spec.fields)


@dataclass
class Checked:
    """What a successful check found: ids to save with the credentials and a friendly line."""

    found: dict[str, str] = field(default_factory=dict)
    message: str = "Connected."


def _client(transport: httpx.BaseTransport | None) -> httpx.Client:
    return httpx.Client(timeout=TIMEOUT, transport=transport, follow_redirects=False)


def _get(http: httpx.Client, url: str, **kwargs: Any) -> Any:
    try:
        response = http.get(url, **kwargs)
    except httpx.HTTPError as exc:
        raise SetupError("Couldn't reach it. Check the address and your internet.") from exc
    if response.status_code in (401, 403):
        raise SetupError("It didn't accept those details. Check them and try again.")
    if response.status_code >= 400:
        raise SetupError(f"It answered with an error ({response.status_code}).")
    try:
        return response.json()
    except ValueError as exc:
        raise SetupError("It sent something unexpected. Check the address.") from exc


def _check_claude(v: dict[str, str], http: httpx.Client, _s: Settings) -> Checked:
    key = v["api_key"]
    if not key.startswith("sk-ant-"):
        raise SetupError("That doesn't look like a Claude key. It starts with sk-ant-")
    _get(
        http,
        "https://api.anthropic.com/v1/models",
        headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
        params={"limit": 1},
    )
    return Checked(message="Claude is ready.")


def _check_jira(v: dict[str, str], http: httpx.Client, _s: Settings) -> Checked:
    base = v["base_url"].strip().rstrip("/")
    if not base.startswith("https://"):
        raise SetupError("Use your full Jira address, starting with https://")
    me = _get(
        http,
        f"{base}/rest/api/3/myself",
        headers=basic_auth_header(v["email"], v["api_token"]),
    )
    return Checked({"base_url": base}, f"Signed in as {me.get('displayName', v['email'])}.")


def _check_asana(v: dict[str, str], http: httpx.Client, _s: Settings) -> Checked:
    me = _get(
        http,
        "https://app.asana.com/api/1.0/users/me",
        headers={"Authorization": f"Bearer {v['token']}"},
    )
    data = me.get("data") or {}
    workspaces = data.get("workspaces") or []
    if not workspaces:
        raise SetupError("That Asana account has no workspace.")
    return Checked(
        {"workspace": str(workspaces[0]["gid"])},
        f"Signed in as {data.get('name', 'you')} ({workspaces[0].get('name', 'workspace')}).",
    )


def _check_clickup(v: dict[str, str], http: httpx.Client, _s: Settings) -> Checked:
    headers = {"Authorization": v["token"]}
    user = (_get(http, "https://api.clickup.com/api/v2/user", headers=headers).get("user")) or {}
    teams = _get(http, "https://api.clickup.com/api/v2/team", headers=headers).get("teams") or []
    if not teams or "id" not in user:
        raise SetupError("Couldn't find your ClickUp workspace.")
    return Checked(
        {"team": str(teams[0]["id"]), "user": str(user["id"])},
        f"Signed in as {user.get('username', 'you')} ({teams[0].get('name', 'workspace')}).",
    )


def _check_telegram(v: dict[str, str], http: httpx.Client, _s: Settings) -> Checked:
    base = f"https://api.telegram.org/bot{v['bot_token']}"
    me = _get(http, f"{base}/getMe")
    name = (me.get("result") or {}).get("username", "your bot")
    chat_id = v.get("chat_id", "").strip()
    if chat_id:
        return Checked({"chat_id": chat_id}, f"Connected to @{name}.")
    updates = _get(http, f"{base}/getUpdates", params={"limit": 20, "timeout": 0})
    for update in reversed(updates.get("result") or []):
        chat = (update.get("message") or {}).get("chat") or {}
        if chat.get("type") == "private" and "id" in chat:
            return Checked({"chat_id": str(chat["id"])}, f"Connected to @{name}.")
    raise SetupError(
        f"Almost there. Open @{name} in Telegram, press Start, then tap Connect again."
    )


def _check_slack(v: dict[str, str], http: httpx.Client, settings: Settings) -> Checked:
    headers = {"Authorization": f"Bearer {v['bot_token']}"}
    auth = _get(http, "https://slack.com/api/auth.test", headers=headers)
    if not auth.get("ok"):
        raise SetupError("Slack didn't accept that token.")
    found: dict[str, str] = {}
    if not v.get("user_id") and settings.ceo_email:
        lookup = _get(
            http,
            "https://slack.com/api/users.lookupByEmail",
            headers=headers,
            params={"email": settings.ceo_email},
        )
        if lookup.get("ok"):
            found["user_id"] = str((lookup.get("user") or {}).get("id", ""))
    return Checked(found, f"Connected to {auth.get('team', 'Slack')}.")


def _check_teams_webhook(v: dict[str, str], _http: httpx.Client, _s: Settings) -> Checked:
    if not v["webhook_url"].startswith("https://"):
        raise SetupError("That doesn't look like a webhook address.")
    return Checked(message="Saved.")


_CHECKS: dict[str, Callable[[dict[str, str], httpx.Client, Settings], Checked]] = {
    "claude": _check_claude,
    "jira": _check_jira,
    "asana": _check_asana,
    "clickup": _check_clickup,
    "telegram": _check_telegram,
    "slack": _check_slack,
    "teams-webhook": _check_teams_webhook,
}

Hook = Callable[[str], None]


class ConnectionService:
    """Encrypted storage plus applying the values to the running ``Settings``."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        cipher: TokenCipher,
        settings: Settings,
        clock: Clock = utc_now,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._sessions = session_factory
        self._cipher = cipher
        self._settings = settings
        self._clock = clock
        self._transport = transport
        self._hooks: list[Hook] = []

    def on_change(self, hook: Hook) -> None:
        self._hooks.append(hook)

    # -- storage ----------------------------------------------------------------------

    def _read(self, app_id: str) -> dict[str, str]:
        with self._sessions() as s:
            row = s.get(AppConnectionRow, app_id)
            if row is None:
                return {}
            try:
                data = json.loads(self._cipher.decrypt(row.data_enc))
            except ValueError:
                log.warning("could not read saved %s credentials; ignoring them", app_id)
                return {}
        return {str(k): str(v) for k, v in data.items()}

    def _write(self, app_id: str, values: dict[str, str]) -> None:
        with self._sessions.begin() as s:
            row = s.get(AppConnectionRow, app_id) or AppConnectionRow(app_id=app_id)
            row.data_enc = self._cipher.encrypt(json.dumps(values))
            row.updated_at = self._clock()
            s.add(row)

    def saved(self, app_id: str) -> dict[str, str]:
        return self._read(app_id)

    def has_saved(self, app_id: str) -> bool:
        return bool(self._read(app_id))

    # -- applying to settings ---------------------------------------------------------

    def _apply(self, spec: AppSpec, values: dict[str, str]) -> None:
        for key, attr in spec.settings.items():
            value = values.get(key, "")
            current = getattr(self._settings, attr)
            if isinstance(current, SecretStr):
                setattr(self._settings, attr, SecretStr(value))
            else:
                setattr(self._settings, attr, value)

    def apply_saved(self) -> None:
        """At startup: saved values win over ``.env``."""
        for spec in APPS:
            values = self._read(spec.id)
            if values:
                self._apply(spec, values)

    def current(self, spec: AppSpec) -> dict[str, str]:
        """What is in effect right now, from the app or from ``.env``."""
        out: dict[str, str] = {}
        for key, attr in spec.settings.items():
            value = getattr(self._settings, attr)
            out[key] = value.get_secret_value() if isinstance(value, SecretStr) else str(value)
        return out

    def is_connected(self, spec: AppSpec) -> bool:
        current = self.current(spec)
        required = [f.key for f in spec.fields if not f.optional]
        return all(current.get(k) for k in required)

    def source(self, spec: AppSpec) -> str | None:
        if self.has_saved(spec.id):
            return "app"
        return "env" if self.is_connected(spec) else None

    # -- actions ----------------------------------------------------------------------

    def connect(self, app_id: str, submitted: dict[str, str]) -> Checked:
        spec = BY_ID[app_id]
        values = dict(self.current(spec)) if self.is_connected(spec) else {}
        for f in spec.fields:
            text = (submitted.get(f.key) or "").strip()
            if text:
                values[f.key] = text
        missing = [f.label for f in spec.fields if not f.optional and not values.get(f.key)]
        if missing:
            raise SetupError("Please fill in: " + ", ".join(missing) + ".")
        with _client(self._transport) as http:
            checked = _CHECKS[app_id](values, http, self._settings)
        values.update({k: v for k, v in checked.found.items() if v})
        self._write(app_id, values)
        self._apply(spec, values)
        self._fire(app_id)
        return checked

    def disconnect(self, app_id: str) -> None:
        spec = BY_ID[app_id]
        with self._sessions.begin() as s:
            s.execute(delete(AppConnectionRow).where(AppConnectionRow.app_id == app_id))
        self._apply(spec, {})
        self._fire(app_id)

    def _fire(self, app_id: str) -> None:
        for hook in self._hooks:
            try:
                hook(app_id)
            except Exception:
                log.exception("connection hook failed for %s", app_id)
