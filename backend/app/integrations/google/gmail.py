"""Gmail adapter (Gmail API v1)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.domain.enums import Source
from app.integrations.base import EmailAdapter, EmailMessage, OutboundEmail, SentEmail
from app.integrations.http import HttpClient
from app.integrations.mime import b64url, bare_address, build_mime

BASE_URL = "https://gmail.googleapis.com/gmail/v1/users/me"
DEFAULT_QUERY = "(is:starred OR is:important) -from:me -category:promotions"
_HEADERS = ["Subject", "From", "To", "Message-ID"]


class GmailAdapter(EmailAdapter):
    source = Source.GMAIL

    def __init__(self, http: HttpClient, query: str = DEFAULT_QUERY) -> None:
        self._http = http
        self._query = query

    def list_actionable(self, since: datetime) -> list[EmailMessage]:
        q = f"{self._query} after:{int(since.timestamp())}"
        ids: list[str] = []
        page_token: str | None = None
        while True:
            params = {"q": q, "maxResults": "100"}
            if page_token:
                params["pageToken"] = page_token
            body = self._http.get_json("/messages", params=params)
            ids.extend(m["id"] for m in body.get("messages", []))
            page_token = body.get("nextPageToken")
            if not page_token:
                break
        return [_message(self._get_metadata(i)) for i in ids]

    def list_thread_replies(self, thread_id: str, since: datetime) -> list[EmailMessage]:
        body = self._http.get_json(
            f"/threads/{thread_id}",
            params={"format": "metadata", "metadataHeaders": _HEADERS},
        )
        replies = []
        for raw in body.get("messages", []):
            msg = _message(raw)
            if "SENT" in raw.get("labelIds", []):
                continue
            if msg.received_at and msg.received_at > since:
                replies.append(msg)
        return replies

    def send(self, message: OutboundEmail) -> SentEmail:
        mime = build_mime(message)
        payload: dict[str, Any] = {}
        if message.reply_to_message_id:
            original = self._get_metadata(message.reply_to_message_id)
            message_id_header = _header(original, "Message-ID")
            if message_id_header:
                mime["In-Reply-To"] = message_id_header
                mime["References"] = message_id_header
            payload["threadId"] = original.get("threadId")
        payload["raw"] = b64url(mime.as_bytes())
        sent = self._http.send_json("POST", "/messages/send", payload)
        thread = sent.get("threadId")
        return SentEmail(str(sent["id"]), str(thread) if thread else None)

    def _get_metadata(self, message_id: str) -> dict[str, Any]:
        result: dict[str, Any] = self._http.get_json(
            f"/messages/{message_id}",
            params={"format": "metadata", "metadataHeaders": _HEADERS},
        )
        return result


def _header(raw: dict[str, Any], name: str) -> str | None:
    for h in raw.get("payload", {}).get("headers", []):
        if h.get("name", "").lower() == name.lower():
            value: str = h.get("value", "")
            return value
    return None


def _message(raw: dict[str, Any]) -> EmailMessage:
    internal = raw.get("internalDate")
    to = _header(raw, "To") or ""
    return EmailMessage(
        source=Source.GMAIL,
        id=raw["id"],
        thread_id=raw.get("threadId"),
        subject=_header(raw, "Subject") or "(no subject)",
        sender=bare_address(_header(raw, "From")),
        recipients=tuple(a for a in (bare_address(x) for x in to.split(",")) if a),
        snippet=raw.get("snippet", ""),
        received_at=datetime.fromtimestamp(int(internal) / 1000, UTC) if internal else None,
        url=f"https://mail.google.com/mail/u/0/#all/{raw.get('threadId', raw['id'])}",
        flagged="STARRED" in raw.get("labelIds", []),
    )
