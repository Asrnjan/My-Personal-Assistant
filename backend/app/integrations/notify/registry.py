"""Builds the notifiers enabled under ``adapters.notify`` in ``config.yaml``."""

from __future__ import annotations

import logging

from app.config import AppConfig, Settings
from app.integrations.notify.base import Notifier
from app.integrations.notify.email import EmailNotifier
from app.integrations.notify.slack import SlackNotifier, slack_client
from app.integrations.notify.teams import TeamsNotifier
from app.integrations.notify.telegram import TelegramApi, TelegramNotifier, bot_client
from app.integrations.registry import Adapters

log = logging.getLogger(__name__)


def telegram_api(settings: Settings) -> TelegramApi | None:
    token = settings.telegram_bot_token.get_secret_value()
    if not token:
        return None
    # Long enough for a getUpdates long poll.
    return TelegramApi(bot_client(token, timeout=60), bot_client(token, files=True, timeout=60))


def build_notifiers(settings: Settings, config: AppConfig, adapters: Adapters) -> list[Notifier]:
    out: list[Notifier] = []
    enabled = config.adapters.notify
    if "email" in enabled:
        if adapters.mail:
            # Delegations go out from the CEO's first connected mailbox.
            out.append(EmailNotifier(adapters.mail[0], settings.ceo_email or None))
        else:
            log.warning("notifier skipped - email: no Google or Microsoft mailbox connected")
    if "telegram" in enabled:
        api = telegram_api(settings)
        if api is not None:
            out.append(TelegramNotifier(api, settings.telegram_ceo_chat_id or None))
        else:
            log.warning("notifier skipped - telegram: TELEGRAM_BOT_TOKEN is not set")
    if "slack" in enabled:
        token = settings.slack_bot_token.get_secret_value()
        if token:
            out.append(SlackNotifier(slack_client(token), settings.slack_ceo_user_id or None))
        else:
            log.warning("notifier skipped - slack: SLACK_BOT_TOKEN is not set")
    if "teams" in enabled:
        # Delegates' webhooks live in contacts.yaml, so Teams works without a CEO webhook.
        out.append(TeamsNotifier(settings.teams_webhook_url.get_secret_value() or None))
    return out
