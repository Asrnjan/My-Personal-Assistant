"""Web Push to the PWA: scheduled briefs, P1 alerts and notices arrive as phone
notifications even when the app is closed.

Keys are VAPID keys you generate yourself (``scripts/generate_vapid_keys.py``);
no paid push service is involved.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import PushSubscriptionRow
from app.events import Event

log = logging.getLogger(__name__)

# Gone or invalid subscriptions; the browser unsubscribed or the app was removed.
_EXPIRED = frozenset({404, 410})
_TITLES = {
    "morning": "Your morning brief",
    "evening": "Your evening wrap-up",
    "alert": "New P1",
    "notice": "Chief of Staff",
}
MAX_BODY = 240

# (subscription_info, json payload) -> HTTP status from the push service
PushSend = Callable[[dict[str, Any], str], int]


def pywebpush_sender(private_key: str, subject: str) -> PushSend:
    def send(subscription: dict[str, Any], payload: str) -> int:
        from pywebpush import WebPushException, webpush

        try:
            webpush(
                subscription,
                payload,
                vapid_private_key=private_key,
                vapid_claims={"sub": subject},  # pywebpush adds aud/exp to this dict
                ttl=12 * 3600,
                timeout=15,
            )
        except WebPushException as exc:
            response = getattr(exc, "response", None)
            return int(getattr(response, "status_code", 0) or 0)
        return 201

    return send


def notification(event: Event) -> dict[str, str] | None:
    """What the phone shows for a bus event, or None when it shouldn't notify."""
    if event.kind == "brief":
        kind = str(event.payload.get("kind", "morning"))
        text = str(event.payload.get("text", ""))
        url = "/?autostart=brief" if kind in ("morning", "evening") else "/"
    elif event.kind == "notice":
        kind, text, url = "notice", str(event.payload.get("text", "")), "/"
    else:
        return None
    text = " ".join(text.split())
    if not text:
        return None
    if len(text) > MAX_BODY:
        text = text[: MAX_BODY - 1].rstrip() + "…"
    return {"title": _TITLES.get(kind, "Chief of Staff"), "body": text, "tag": kind, "url": url}


class WebPusher:
    def __init__(self, session_factory: sessionmaker[Session], send: PushSend) -> None:
        self._sessions = session_factory
        self._send = send

    def subscribe(self, endpoint: str, p256dh: str, auth: str, client_id: str | None) -> None:
        with self._sessions.begin() as s:
            row = s.get(PushSubscriptionRow, endpoint)
            if row is None:
                s.add(
                    PushSubscriptionRow(
                        endpoint=endpoint, p256dh=p256dh, auth=auth, client_id=client_id
                    )
                )
            else:
                row.p256dh, row.auth, row.client_id = p256dh, auth, client_id

    def unsubscribe(self, endpoint: str) -> bool:
        with self._sessions.begin() as s:
            row = s.get(PushSubscriptionRow, endpoint)
            if row is None:
                return False
            s.delete(row)
            return True

    def count(self) -> int:
        with self._sessions() as s:
            return len(list(s.scalars(select(PushSubscriptionRow.endpoint))))

    def push(self, message: dict[str, str]) -> int:
        """Send to every subscription; drop expired ones. Returns how many accepted it."""
        with self._sessions() as s:
            subs: list[dict[str, Any]] = [
                {"endpoint": r.endpoint, "keys": {"p256dh": r.p256dh, "auth": r.auth}}
                for r in s.scalars(select(PushSubscriptionRow))
            ]
        payload = json.dumps(message)
        delivered = 0
        for sub in subs:
            try:
                status = self._send(sub, payload)
            except Exception:
                log.exception("web push failed")
                continue
            if status in _EXPIRED:
                self.unsubscribe(str(sub["endpoint"]))
            elif 200 <= status < 300:
                delivered += 1
            else:
                log.warning("web push rejected with status %s", status)
        return delivered

    def on_event(self, event: Event) -> None:
        message = notification(event)
        if message is not None:
            self.push(message)
