"""Offline stand-in for the Claude API, used by tests and the demo seed script."""

from __future__ import annotations

import json
import re
import uuid
from collections import deque
from collections.abc import Callable, Iterable
from typing import Any

from anthropic.types import Message

Responder = Callable[[dict[str, Any]], Message]


def fake_message(
    text: str | dict[str, Any] | list[Any] = "",
    *,
    model: str = "claude-haiku-5-5",
    stop_reason: str = "end_turn",
    input_tokens: int = 100,
    output_tokens: int = 20,
    cache_creation_input_tokens: int = 0,
    cache_read_input_tokens: int = 0,
    tool_uses: Iterable[tuple[str, dict[str, Any]]] = (),
    refusal_category: str | None = None,
) -> Message:
    """Build a ``Message`` as the API would return it. Dicts and lists become JSON text."""
    body = text if isinstance(text, str) else json.dumps(text)
    content: list[dict[str, Any]] = []
    if body:
        content.append({"type": "text", "text": body})
    for name, tool_input in tool_uses:
        content.append(
            {
                "type": "tool_use",
                "id": f"toolu_{uuid.uuid4().hex[:12]}",
                "name": name,
                "input": tool_input,
            }
        )
    data: dict[str, Any] = {
        "id": f"msg_{uuid.uuid4().hex[:12]}",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_creation_input_tokens": cache_creation_input_tokens,
            "cache_read_input_tokens": cache_read_input_tokens,
        },
    }
    if stop_reason == "refusal":
        data["stop_details"] = {
            "type": "refusal",
            "category": refusal_category,
            "explanation": None,
        }
    return Message.model_validate(data)


class FakeSender:
    """Returns queued responses in order and records every request it receives.

    A queued item is either a ``Message`` or a function from the request
    parameters to a ``Message``. With an empty queue, ``default`` is used.
    """

    def __init__(
        self,
        responses: Iterable[Message | Responder] = (),
        default: Message | Responder | None = None,
    ) -> None:
        self.calls: list[dict[str, Any]] = []
        self._queue: deque[Message | Responder] = deque(responses)
        self._default = default

    def queue(self, *responses: Message | Responder) -> None:
        self._queue.extend(responses)

    def stream(
        self,
        params: dict[str, Any],
        on_text: Callable[[str], None],
        on_tool: Callable[[str], None] | None = None,
    ) -> Message:
        """Like ``send``, handing the reply's text over word by word as a stream would,
        and each tool's name as its call starts."""
        message = self.send(params)
        for block in message.content:
            if block.type == "text":
                for piece in re.findall(r"\S+\s*", block.text):
                    on_text(piece)
            elif block.type == "tool_use" and on_tool is not None:
                on_tool(block.name)
        return message

    def send(self, params: dict[str, Any]) -> Message:
        self.calls.append(params)
        if self._queue:
            item = self._queue.popleft()
        elif self._default is not None:
            item = self._default
        else:
            raise AssertionError("FakeSender has no response queued for this request")
        return item if isinstance(item, Message) else item(params)


_ITEM_ID = re.compile(r'"id": "([^"]+)"')


def echo_scores(score: int = 50, band: str = "P2_delegate", confidence: float = 0.9) -> Responder:
    """A responder that scores every item found in a priority prompt the same way."""

    def respond(params: dict[str, Any]) -> Message:
        user = params["messages"][0]["content"]
        scores = [
            {
                "item_id": item_id,
                "score": score,
                "band": band,
                "reason": "Scored by the offline stand-in.",
                "confidence": confidence,
                "ceo_required": band == "P1_do_myself",
                "suggested_delegate_id": None,
            }
            for item_id in _ITEM_ID.findall(user)
        ]
        return fake_message({"scores": scores}, model=params["model"])

    return respond


def offline_responder(score: int = 50, band: str = "P2_delegate") -> Responder:
    """Answers every kind of request the backend makes, without a network.

    Scoring prompts get :func:`echo_scores`, delegation briefs a short brief
    built from the item title, acknowledgement checks a keyword verdict, and
    conversation turns a polite note that free-form requests need Claude.
    Used by the offline demo and by tests that don't script the model.
    """
    scores = echo_scores(score=score, band=band)

    def respond(params: dict[str, Any]) -> Message:
        system = " ".join(b.get("text", "") for b in params.get("system", []))
        first = params["messages"][0]["content"]
        user = first if isinstance(first, str) else json.dumps(first)
        model = params["model"]
        if "delegation briefs" in system:
            title = re.search(r"Title: (.*)", user)
            subject = (title.group(1) if title else "A task for you").strip()[:80]
            return fake_message(
                {
                    "subject": subject,
                    "task_summary": f"Please take over {subject}.",
                    "why_it_matters": "It needs an owner this week.",
                    "expected_outcome": "It's done and you've let me know.",
                    "deadline": None,
                    "context_links": [],
                    "handoff_type": "full_ownership",
                },
                model=model,
            )
        if "classify them" in system:
            accepted = bool(re.search(r"\b(accept(ed)?|ok(ay)?|on it|will do)\b", user, re.I))
            return fake_message(
                {"acknowledged": accepted, "has_question": "?" in user, "confidence": 0.9},
                model=model,
            )
        if "prepare a CEO for a meeting" in system:
            meeting = re.search(r"Meeting: (.*)", user)
            who = re.search(r"Attendees: (.*)", user)
            return fake_message(
                f"{(meeting.group(1) if meeting else 'Your meeting').strip()} is with "
                f"{(who.group(1) if who else 'your team').strip()}.",
                model=model,
            )
        if "draft short email replies" in system:
            ids = re.findall(r"item_id: (\S+)", user)
            drafts = [
                {"item_id": i, "reply": "Thanks, I'll come back to you on this."} for i in ids
            ]
            return fake_message({"drafts": drafts}, model=model)
        if "meeting notes or a meeting transcript" in system:
            return fake_message({"summary": "Noted.", "decisions": [], "actions": []}, model=model)
        if "find promises the CEO made" in system:
            return fake_message({"promises": []}, model=model)
        if "describe how they write" in system:
            return fake_message("Short and direct. Opens with Hi, signs off Thanks.", model=model)
        if "thinking partner" in system:
            return fake_message("I'd go ahead, but check the budget first.", model=model)
        if "You write for a CEO in their own voice" in system:
            return fake_message("[draft]", model=model)
        if "You write presentations for a CEO" in system:
            found = re.search(r"Topic: (.*)", user)
            deck_title = (found.group(1) if found else "Update").strip()
            slide = {"title": "The point", "bullets": ["One", "Two"], "notes": "Say this."}
            return fake_message(
                {"title": deck_title, "subtitle": "Prepared by Atlas", "slides": [slide, slide]},
                model=model,
            )
        if params.get("tools"):
            return fake_message(
                "I'm running offline, so I can only handle brief me, yes, cancel and skip.",
                model=model,
            )
        return scores(params)

    return respond
