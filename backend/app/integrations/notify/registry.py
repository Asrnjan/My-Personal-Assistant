"""Builds the notifiers enabled under ``adapters.notify`` in ``config.yaml``."""

from __future__ import annotations

import logging

from app.config import AppConfig, Settings
from app.integrations.notify.base import Notifier
from app.integrations.notify.email import EmailNotifier
from app.integrations.registry import Adapters

log = logging.getLogger(__name__)


def build_notifiers(settings: Settings, config: AppConfig, adapters: Adapters) -> list[Notifier]:
    out: list[Notifier] = []
    enabled = config.adapters.notify
    if "email" in enabled:
        if adapters.mail:
            # Delegations go out from the CEO's first connected mailbox.
            out.append(EmailNotifier(adapters.mail[0], settings.ceo_email or None))
        else:
            log.warning("notifier skipped - email: no Google or Microsoft mailbox connected")
    return out
