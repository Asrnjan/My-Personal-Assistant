"""Claude API wrapper: model routing, prompt caching, structured output,
refusal handling, usage logging and budget enforcement.

All calls go through :class:`LLMClient`. The network is behind the
:class:`MessageSender` protocol so tests and the offline demo can swap in
:class:`app.llm.fake.FakeSender`.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from enum import StrEnum
from typing import Any, Protocol, TypeVar, cast

import anthropic
from anthropic.types import Message, MessageParam
from pydantic import BaseModel, ValidationError

from app.config import ModelsConfig
from app.llm.errors import (
    LLMAuthError,
    LLMOutputError,
    LLMRefusalError,
    LLMTruncatedError,
    LLMUnavailableError,
)
from app.llm.prompts import load_prompt
from app.llm.schemas import strict_json_schema
from app.llm.usage import BudgetGuard, UsageLedger

T = TypeVar("T", bound=BaseModel)

# Models that accept the server-side refusal fallback in its "default" form.
_FALLBACK_MODELS = frozenset(
    {"claude-sonnet-5-5", "claude-opus-5-5", "claude-opus-5", "claude-fable-5-1"}
)
FALLBACK_BETA = "server-side-fallback-2026-07-01"


class Task(StrEnum):
    """What a call is for; decides the model and effort."""

    BULK = "bulk"  # scoring, classification
    CONVERSATION = "conversation"  # dialogue, delegation briefs, hard judgment calls


TextHandler = Callable[[str], None]


class MessageSender(Protocol):
    def send(self, params: dict[str, Any]) -> Message: ...


class AnthropicSender:
    """Sends requests with the official SDK, which retries 408/409/429/5xx and
    connection errors with exponential backoff."""

    def __init__(self, api_key: str | None, max_retries: int) -> None:
        self._api_key = api_key or None
        self._max_retries = max_retries
        self._client: anthropic.Anthropic | None = None

    def set_api_key(self, api_key: str | None) -> None:
        """Use a key entered in the app from the next request on."""
        self._api_key = api_key or None
        self._client = None

    def _ensure_client(self) -> anthropic.Anthropic:
        if self._client is None:
            # Created lazily so the app starts without credentials; the SDK
            # falls back to ANTHROPIC_API_KEY or an `ant auth login` profile.
            self._client = anthropic.Anthropic(api_key=self._api_key, max_retries=self._max_retries)
        return self._client

    def send(self, params: dict[str, Any]) -> Message:
        with _api_errors():
            response: Message = self._ensure_client().messages.create(**params)
        return response

    def stream(self, params: dict[str, Any], on_text: TextHandler) -> Message:
        """Like ``send``, calling ``on_text`` with each piece of text as it streams in."""
        with _api_errors(), self._ensure_client().messages.stream(**params) as stream:
            for text in stream.text_stream:
                on_text(text)
            final = stream.get_final_message()
        return cast(Message, final)


@contextmanager
def _api_errors() -> Iterator[None]:
    """Turn the SDK's errors into LLM errors, so every caller's fallback applies."""
    try:
        yield
    except TypeError as exc:
        # The SDK raises this before sending when it finds no credentials at all.
        if "authentication" not in str(exc).lower():
            raise
        raise LLMAuthError("no Claude API key is set") from exc
    except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as exc:
        raise LLMAuthError("the Claude API key was rejected") from exc
    except anthropic.APIError as exc:
        raise LLMUnavailableError(f"Claude API error: {exc}") from exc


# Models that accept thinking "between_tools" (thinking off except short progress notes).
_BETWEEN_TOOLS_MODELS = frozenset({"claude-sonnet-5-5"})
# Models with the web search tool that filters results with code (newer variant).
_DYNAMIC_WEB_SEARCH_MODELS = (
    "claude-opus-5",
    "claude-opus-4-8",
    "claude-opus-4-7",
    "claude-opus-4-6",
    "claude-sonnet-5",
    "claude-sonnet-4-6",
)


class LLMClient:
    def __init__(
        self,
        sender: MessageSender,
        models: ModelsConfig,
        ledger: UsageLedger,
        budget: BudgetGuard,
    ) -> None:
        self._sender = sender
        self._models = models
        self._ledger = ledger
        self._budget = budget

    def web_search_tool(self, task: Task, timezone: str | None = None) -> dict[str, Any] | None:
        """The server-side web search tool for this task's model, or None when it's off."""
        if not self._models.web_search:
            return None
        model = self.model_for(task)
        newer = model.startswith(_DYNAMIC_WEB_SEARCH_MODELS)
        tool: dict[str, Any] = {
            "type": "web_search_20260209" if newer else "web_search_20250305",
            "name": "web_search",
            "max_uses": self._models.web_searches_per_turn,
        }
        if timezone:
            tool["user_location"] = {"type": "approximate", "timezone": timezone}
        return tool

    def model_for(self, task: Task) -> str:
        return self._models.bulk if task is Task.BULK else self._models.conversation

    def _effort_for(self, task: Task) -> str:
        return self._models.bulk_effort if task is Task.BULK else self._models.conversation_effort

    def build_params(
        self,
        task: Task,
        *,
        system: Sequence[str],
        messages: Sequence[MessageParam],
        max_tokens: int,
        output_schema: dict[str, Any] | None = None,
        tools: Sequence[dict[str, Any]] | None = None,
        effort: str | None = None,
    ) -> dict[str, Any]:
        """Assemble request parameters.

        ``system`` holds the static parts (instructions, rubric, delegation
        map, CEO preferences), in a fixed order. The safety preamble is
        prepended and a cache breakpoint goes on the last system block, so
        everything static is cached and only ``messages`` vary per call.
        """
        texts = [load_prompt("safety").text, *system]
        blocks: list[dict[str, Any]] = [{"type": "text", "text": t} for t in texts]
        blocks[-1]["cache_control"] = {"type": "ephemeral"}

        output_config: dict[str, Any] = {"effort": effort or self._effort_for(task)}
        if output_schema is not None:
            output_config["format"] = {"type": "json_schema", "schema": output_schema}

        model = self.model_for(task)
        params: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "system": blocks,
            "messages": list(messages),
            "output_config": output_config,
        }
        if (
            task is Task.CONVERSATION
            and self._models.fast_replies
            and model in _BETWEEN_TOOLS_MODELS
            and output_config["effort"] in ("low", "medium", "high")
        ):
            # No extended thinking before answering: the biggest single cut in reply time.
            params["thinking"] = {"type": "between_tools"}
        if tools:
            params["tools"] = list(tools)
        if (
            task is Task.CONVERSATION
            and self._models.refusal_fallback
            and model in _FALLBACK_MODELS
        ):
            params["extra_headers"] = {"anthropic-beta": FALLBACK_BETA}
            params["extra_body"] = {"fallbacks": "default"}
        return params

    def call(
        self,
        task: Task,
        purpose: str,
        *,
        system: Sequence[str],
        messages: Sequence[MessageParam],
        max_tokens: int = 4096,
        output_schema: dict[str, Any] | None = None,
        tools: Sequence[dict[str, Any]] | None = None,
        effort: str | None = None,
        on_text: TextHandler | None = None,
    ) -> Message:
        """Send one request. Raises ``BudgetExceededError`` before sending when
        the monthly cap is reached, and ``LLMRefusalError`` / ``LLMTruncatedError``
        on those stop reasons. Usage is recorded for every response."""
        self._budget.check()
        params = self.build_params(
            task,
            system=system,
            messages=messages,
            max_tokens=max_tokens,
            output_schema=output_schema,
            tools=tools,
            effort=effort,
        )
        stream = getattr(self._sender, "stream", None)
        if on_text is not None and stream is not None:
            # Text arrives as it is written, so a voice reply can start before it's done.
            response = cast(Message, stream(params, on_text))
        else:
            response = self._sender.send(params)
        usage = response.usage
        self._ledger.record(
            model=response.model or params["model"],
            purpose=purpose,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_creation_input_tokens=usage.cache_creation_input_tokens or 0,
            cache_read_input_tokens=usage.cache_read_input_tokens or 0,
            request_id=getattr(response, "_request_id", None),
        )
        if response.stop_reason == "refusal":
            category = response.stop_details.category if response.stop_details else None
            raise LLMRefusalError(f"model declined the request (category: {category})")
        if response.stop_reason == "max_tokens":
            raise LLMTruncatedError(f"response hit max_tokens={max_tokens}")
        return response

    def structured(
        self,
        task: Task,
        purpose: str,
        *,
        system: Sequence[str],
        user: str,
        schema: type[T],
        max_tokens: int = 4096,
        repair_attempts: int = 1,
    ) -> T:
        """Ask for output matching ``schema`` and return it validated.

        If the reply is not valid JSON for the schema, the error is sent back
        and the model gets ``repair_attempts`` more tries before
        ``LLMOutputError`` is raised.
        """
        json_schema = strict_json_schema(schema)
        messages: list[MessageParam] = [{"role": "user", "content": user}]
        last_error = ""
        for _ in range(repair_attempts + 1):
            response = self.call(
                task,
                purpose,
                system=system,
                messages=messages,
                max_tokens=max_tokens,
                output_schema=json_schema,
            )
            text = "".join(b.text for b in response.content if b.type == "text")
            try:
                return schema.model_validate(json.loads(text))
            except (json.JSONDecodeError, ValidationError) as exc:
                last_error = str(exc).splitlines()[0]
                messages = [
                    *messages,
                    {"role": "assistant", "content": response.content},
                    {
                        "role": "user",
                        "content": (
                            f"Your reply did not match the required JSON schema ({last_error}). "
                            "Reply again with only the corrected JSON."
                        ),
                    },
                ]
        raise LLMOutputError(f"{purpose}: invalid structured output after retries: {last_error}")
