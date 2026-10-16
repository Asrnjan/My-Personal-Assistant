"""Offline stand-in for the Claude API, used by tests and the demo seed script."""

from __future__ import annotations

import json
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

    def send(self, params: dict[str, Any]) -> Message:
        self.calls.append(params)
        if self._queue:
            item = self._queue.popleft()
        elif self._default is not None:
            item = self._default
        else:
            raise AssertionError("FakeSender has no response queued for this request")
        return item if isinstance(item, Message) else item(params)
