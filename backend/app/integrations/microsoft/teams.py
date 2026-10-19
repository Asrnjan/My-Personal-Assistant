"""Microsoft Teams chats (Microsoft Graph), read and sent as the CEO.

Needs a work or school account and the ``Chat.ReadWrite`` scope, which the
Teams account in Settings → Accounts asks for.
"""

from __future__ import annotations

import html
import re
from typing import Any

from app.integrations.base import Chat, ChatAdapter, ChatMessage
from app.integrations.http import HttpClient
from app.integrations.microsoft.graph import BASE_URL
from app.integrations.mime import parse_iso

_TAGS = re.compile(r"<[^>]+>")
_MEMBER = "#microsoft.graph.aadUserConversationMember"


def plain_text(content: str) -> str:
    """Teams message bodies are HTML; voice needs plain words."""
    text = re.sub(r"<br\s*/?>|</p>|</div>", " ", content, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", html.unescape(_TAGS.sub("", text))).strip()


class TeamsChatAdapter(ChatAdapter):
    def __init__(self, http: HttpClient) -> None:
        self._http = http
        self._me: dict[str, str] | None = None

    def _myself(self) -> dict[str, str]:
        if self._me is None:
            body = self._http.get_json("/me", params={"$select": "id,mail,userPrincipalName"})
            self._me = {
                "id": str(body["id"]),
                "email": str(body.get("mail") or body.get("userPrincipalName") or "").lower(),
            }
        return self._me

    def recent_chats(self, limit: int = 10) -> list[Chat]:
        body = self._http.get_json(
            "/me/chats",
            params={
                "$expand": "members,lastMessagePreview",
                "$orderby": "lastMessagePreview/createdDateTime desc",
                "$top": str(min(limit, 50)),
            },
        )
        me = self._myself()
        return [_chat(c, me["id"]) for c in body.get("value", [])][:limit]

    def messages(self, chat_id: str, limit: int = 10) -> list[ChatMessage]:
        body = self._http.get_json(f"/chats/{chat_id}/messages", params={"$top": str(limit)})
        found = [_message(m) for m in body.get("value", []) if m.get("messageType") == "message"]
        found.sort(key=lambda m: m.sent_at.timestamp() if m.sent_at else 0, reverse=True)
        return found[:limit]

    def send(self, chat_id: str, text: str) -> ChatMessage:
        sent = self._http.send_json(
            "POST",
            f"/chats/{chat_id}/messages",
            {"body": {"contentType": "text", "content": text}},
        )
        return _message(sent or {"id": "", "body": {"content": text}})

    def send_to_person(self, email: str, text: str) -> ChatMessage:
        me = self._myself()
        # Creating a one-on-one chat that already exists returns the existing one.
        chat = self._http.send_json(
            "POST",
            "/chats",
            {
                "chatType": "oneOnOne",
                "members": [
                    {
                        "@odata.type": _MEMBER,
                        "roles": ["owner"],
                        "user@odata.bind": f"{BASE_URL}/users('{me['id']}')",
                    },
                    {
                        "@odata.type": _MEMBER,
                        "roles": ["owner"],
                        "user@odata.bind": f"{BASE_URL}/users('{email}')",
                    },
                ],
            },
        )
        return self.send(str(chat["id"]), text)


def _chat(c: dict[str, Any], my_id: str) -> Chat:
    others = [m for m in c.get("members", []) if m.get("userId") != my_id]
    preview = c.get("lastMessagePreview") or None
    last = None
    if preview:
        sender = ((preview.get("from") or {}).get("user") or {}).get("displayName")
        last = ChatMessage(
            id=str(preview.get("id", "")),
            sender=sender,
            text=plain_text((preview.get("body") or {}).get("content", "")),
            sent_at=parse_iso(preview.get("createdDateTime")),
        )
    return Chat(
        id=str(c["id"]),
        topic=c.get("topic"),
        members=tuple(str(m.get("displayName") or m.get("email") or "") for m in others),
        member_emails=tuple(str(m["email"]).lower() for m in others if m.get("email")),
        last_message=last,
    )


def _message(m: dict[str, Any]) -> ChatMessage:
    sender = ((m.get("from") or {}).get("user") or {}).get("displayName")
    return ChatMessage(
        id=str(m.get("id", "")),
        sender=sender,
        text=plain_text((m.get("body") or {}).get("content", "")),
        sent_at=parse_iso(m.get("createdDateTime")),
    )
