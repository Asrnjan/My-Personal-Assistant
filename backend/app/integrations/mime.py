"""Helpers shared by the mail adapters."""

from __future__ import annotations

import base64
import re
from datetime import UTC, date, datetime
from email.message import EmailMessage as MimeMessage

from app.integrations.base import OutboundEmail

_ADDRESS = re.compile(r"<([^>]+)>")


def build_mime(message: OutboundEmail, sender: str | None = None) -> MimeMessage:
    mime = MimeMessage()
    if sender:
        mime["From"] = sender
    mime["To"] = ", ".join(message.to)
    if message.cc:
        mime["Cc"] = ", ".join(message.cc)
    mime["Subject"] = message.subject
    mime.set_content(message.text)
    if message.html:
        mime.add_alternative(message.html, subtype="html")
    return mime


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def bare_address(header: str | None) -> str | None:
    """'Rahul Kumar <rahul@x.com>' -> 'rahul@x.com'."""
    if not header:
        return None
    match = _ADDRESS.search(header)
    return (match.group(1) if match else header).strip().lower()


def parse_iso(value: str | None) -> datetime | None:
    """Parse an ISO-8601 timestamp; values without an offset are taken as UTC."""
    if not value:
        return None
    text = value.replace("Z", "+00:00")
    # Graph returns 7 fractional digits; Python accepts at most 6.
    text = re.sub(r"(\.\d{6})\d+", r"\1", text)
    parsed = datetime.fromisoformat(text)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def parse_date(value: str | None) -> datetime | None:
    """'2026-10-19' -> midnight UTC on that day."""
    if not value:
        return None
    d = date.fromisoformat(value)
    return datetime(d.year, d.month, d.day, tzinfo=UTC)
