"""Outlook mail adapter (Microsoft Graph)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.domain.enums import Source
from app.integrations.base import EmailAdapter, EmailMessage, OutboundEmail
from app.integrations.http import HttpClient
from app.integrations.microsoft.graph import iterate_pages
from app.integrations.mime import parse_iso

_SELECT = "id,conversationId,subject,from,toRecipients,bodyPreview,receivedDateTime,webLink,flag"


def _odata_time(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class OutlookMailAdapter(EmailAdapter):
    source = Source.OUTLOOK

    def __init__(self, http: HttpClient, ceo_address: str | None = None) -> None:
        self._http = http
        self._ceo = ceo_address.lower() if ceo_address else None

    def list_actionable(self, since: datetime) -> list[EmailMessage]:
        flt = (
            f"receivedDateTime ge {_odata_time(since)} and "
            "(flag/flagStatus eq 'flagged' or importance eq 'high')"
        )
        params = {"$filter": flt, "$select": _SELECT, "$top": "50"}
        return [
            _message(m)
            for page in iterate_pages(self._http, "/me/mailFolders/inbox/messages", params)
            for m in page.get("value", [])
        ]

    def list_thread_replies(self, thread_id: str, since: datetime) -> list[EmailMessage]:
        safe_id = thread_id.replace("'", "''")
        params = {
            "$filter": (
                f"conversationId eq '{safe_id}' and receivedDateTime gt {_odata_time(since)}"
            ),
            "$select": _SELECT,
        }
        messages = [
            _message(m)
            for page in iterate_pages(self._http, "/me/messages", params)
            for m in page.get("value", [])
        ]
        return [m for m in messages if not self._ceo or m.sender != self._ceo]

    def send(self, message: OutboundEmail) -> str:
        body: dict[str, Any] = {
            "subject": message.subject,
            "body": {
                "contentType": "HTML" if message.html else "Text",
                "content": message.html or message.text,
            },
            "toRecipients": [{"emailAddress": {"address": a}} for a in message.to],
            "ccRecipients": [{"emailAddress": {"address": a}} for a in message.cc],
        }
        if message.reply_to_message_id:
            self._http.send_json(
                "POST", f"/me/messages/{message.reply_to_message_id}/reply", {"message": body}
            )
        else:
            self._http.send_json("POST", "/me/sendMail", {"message": body, "saveToSentItems": True})
        return ""  # Graph's send endpoints return 202 with no message id


def _message(m: dict[str, Any]) -> EmailMessage:
    sender = ((m.get("from") or {}).get("emailAddress") or {}).get("address")
    return EmailMessage(
        source=Source.OUTLOOK,
        id=m["id"],
        thread_id=m.get("conversationId"),
        subject=m.get("subject") or "(no subject)",
        sender=sender.lower() if sender else None,
        recipients=tuple(r["emailAddress"]["address"].lower() for r in m.get("toRecipients", [])),
        snippet=m.get("bodyPreview", ""),
        received_at=parse_iso(m.get("receivedDateTime")),
        url=m.get("webLink"),
        flagged=(m.get("flag") or {}).get("flagStatus") == "flagged",
    )
