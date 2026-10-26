from __future__ import annotations

import types
from collections.abc import Callable
from datetime import UTC, datetime

import anthropic
import httpx
import pytest
from anthropic.types import Message

from app.domain.enums import PriorityBand
from app.llm.client import FALLBACK_BETA, AnthropicSender, LLMClient, Task
from app.llm.errors import (
    BudgetExceededError,
    LLMError,
    LLMOutputError,
    LLMRefusalError,
    LLMTruncatedError,
)
from app.llm.fake import FakeSender, fake_message
from app.llm.prompts import load_prompt
from app.llm.schemas import AckClassification, ScoreBatch
from app.llm.usage import UsageLedger

SCORE = {
    "scores": [
        {
            "item_id": "i1",
            "score": 92,
            "band": "P1_do_myself",
            "reason": "The board chair asked for it by Friday.",
            "confidence": 0.9,
            "ceo_required": True,
            "suggested_delegate_id": None,
        }
    ]
}
SINCE = datetime(2026, 10, 1, tzinfo=UTC)


def test_routing_effort_and_cache_placement(
    make_llm: Callable[..., LLMClient], sender: FakeSender
) -> None:
    llm = make_llm()
    sender.queue(fake_message(SCORE))
    llm.structured(
        Task.BULK, "score", system=["RUBRIC", "PREFERENCES"], user="items...", schema=ScoreBatch
    )
    params = sender.calls[0]
    assert params["model"] == "claude-haiku-5-5"
    assert params["output_config"]["effort"] == "low"
    assert params["output_config"]["format"]["type"] == "json_schema"
    system = params["system"]
    assert [b["text"] for b in system] == [load_prompt("safety").text, "RUBRIC", "PREFERENCES"]
    # Exactly one breakpoint, on the last static block; the variable user text is uncached.
    assert [("cache_control" in b) for b in system] == [False, False, True]
    assert system[-1]["cache_control"] == {"type": "ephemeral"}
    assert params["messages"] == [{"role": "user", "content": "items..."}]
    assert "extra_body" not in params


def test_conversation_is_fast_and_reasoning_uses_sonnet_with_refusal_fallback(
    make_llm: Callable[..., LLMClient], sender: FakeSender
) -> None:
    llm = make_llm()
    talk = llm.build_params(Task.CONVERSATION, system=[], messages=[], max_tokens=10)
    assert talk["model"] == "claude-haiku-5-5"
    assert talk["thinking"] == {"type": "disabled"}  # no extended thinking before replying
    assert "extra_body" not in talk  # no server-side fallback for Haiku
    params = llm.build_params(
        Task.REASONING,
        system=["S"],
        messages=[{"role": "user", "content": "hi"}],
        max_tokens=100,
        tools=[{"name": "list_items", "input_schema": {"type": "object"}}],
    )
    assert params["model"] == "claude-sonnet-5-5"
    assert params["output_config"] == {"effort": "low"}
    assert params["extra_headers"] == {"anthropic-beta": FALLBACK_BETA}
    assert params["extra_body"] == {"fallbacks": "default"}
    assert params["tools"][0]["name"] == "list_items"
    assert "thinking" not in params  # reasoning keeps adaptive thinking

    off = make_llm(refusal_fallback=False).build_params(
        Task.REASONING, system=[], messages=[], max_tokens=10
    )
    assert "extra_body" not in off


def test_structured_parses_and_logs_usage(
    make_llm: Callable[..., LLMClient], sender: FakeSender, ledger: UsageLedger
) -> None:
    sender.queue(fake_message(SCORE, input_tokens=1000, cache_read_input_tokens=3000))
    batch = make_llm().structured(Task.BULK, "score", system=["R"], user="u", schema=ScoreBatch)
    assert batch.scores[0].band == PriorityBand.P1_DO_MYSELF
    totals = ledger.totals_since(SINCE)
    assert totals.calls == 1
    assert totals.cache_read_input_tokens == 3000
    assert totals.cost_usd > 0


def test_invalid_json_is_repaired_once(
    make_llm: Callable[..., LLMClient], sender: FakeSender, ledger: UsageLedger
) -> None:
    sender.queue(fake_message("not json"), fake_message(SCORE))
    batch = make_llm().structured(Task.BULK, "score", system=["R"], user="u", schema=ScoreBatch)
    assert batch.scores[0].item_id == "i1"
    retry = sender.calls[1]["messages"]
    assert [m["role"] for m in retry] == ["user", "assistant", "user"]
    assert "did not match" in retry[2]["content"]
    assert ledger.totals_since(SINCE).calls == 2


def test_schema_violation_after_retries_raises(
    make_llm: Callable[..., LLMClient], sender: FakeSender
) -> None:
    bad = {"acknowledged": "maybe", "has_question": False, "confidence": 2}
    sender.queue(fake_message(bad), fake_message(bad))
    with pytest.raises(LLMOutputError, match="ack"):
        make_llm().structured(Task.BULK, "ack", system=[], user="u", schema=AckClassification)
    assert len(sender.calls) == 2


def test_refusal_and_truncation(make_llm: Callable[..., LLMClient], sender: FakeSender) -> None:
    llm = make_llm()
    sender.queue(fake_message(stop_reason="refusal", refusal_category="cyber"))
    with pytest.raises(LLMRefusalError, match="cyber"):
        llm.call(Task.CONVERSATION, "c", system=[], messages=[{"role": "user", "content": "x"}])
    sender.queue(fake_message("partial", stop_reason="max_tokens"))
    with pytest.raises(LLMTruncatedError):
        llm.call(Task.BULK, "c", system=[], messages=[{"role": "user", "content": "x"}])


def test_budget_exceeded_blocks_before_sending(
    make_llm: Callable[..., LLMClient], sender: FakeSender, ledger: UsageLedger
) -> None:
    ledger.record(model="claude-sonnet-5-5", purpose="x", input_tokens=0, output_tokens=1_000_000)
    with pytest.raises(BudgetExceededError):
        make_llm(cap=5.0).call(Task.BULK, "c", system=[], messages=[])
    assert sender.calls == []


def test_tool_use_response_passes_through(
    make_llm: Callable[..., LLMClient], sender: FakeSender
) -> None:
    sender.queue(
        fake_message("", tool_uses=[("decline_meeting", {"item_id": "m1"})], stop_reason="tool_use")
    )
    msg = make_llm().call(Task.CONVERSATION, "c", system=[], messages=[])
    assert msg.content[0].type == "tool_use"


def test_fake_sender_default_responder_and_empty_queue() -> None:
    def echo(params: dict[str, object]) -> Message:
        return fake_message(f"model={params['model']}")

    sender = FakeSender(default=echo)
    assert sender.send({"model": "m"}).content[0].type == "text"
    with pytest.raises(AssertionError):
        FakeSender().send({})


def test_anthropic_sender_uses_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    created: list[dict[str, object]] = []

    class FakeMessages:
        def create(self, **params: object) -> Message:
            return fake_message("ok")

    class FakeAnthropic:
        def __init__(self, **kwargs: object) -> None:
            created.append(kwargs)
            self.messages = FakeMessages()

    monkeypatch.setattr("app.llm.client.anthropic.Anthropic", FakeAnthropic)
    sender = AnthropicSender("", max_retries=4)
    assert created == []  # lazy: no client until the first call
    sender.send({"model": "m"})
    sender.send({"model": "m"})
    assert created == [{"api_key": None, "max_retries": 4}]


@pytest.mark.parametrize(
    ("raised", "expected"),
    [
        (TypeError("Could not resolve authentication method."), "LLMAuthError"),
        (
            anthropic.AuthenticationError(
                "bad key",
                response=httpx.Response(401, request=httpx.Request("POST", "https://x")),
                body=None,
            ),
            "LLMAuthError",
        ),
        (
            anthropic.APIConnectionError(request=httpx.Request("POST", "https://x")),
            "LLMUnavailableError",
        ),
    ],
)
def test_sender_turns_api_failures_into_llm_errors(
    monkeypatch: pytest.MonkeyPatch, raised: Exception, expected: str
) -> None:
    class Failing:
        def __init__(self, **_kwargs: object) -> None:
            self.messages = self

        def create(self, **_params: object) -> None:
            raise raised

    monkeypatch.setattr("app.llm.client.anthropic.Anthropic", Failing)
    with pytest.raises(LLMError) as caught:
        AnthropicSender(None, max_retries=0).send({"model": "m"})
    assert type(caught.value).__name__ == expected


def test_other_type_errors_are_not_hidden(monkeypatch: pytest.MonkeyPatch) -> None:
    class Broken:
        def __init__(self, **_kwargs: object) -> None:
            self.messages = self

        def create(self, **_params: object) -> None:
            raise TypeError("unexpected keyword")

    monkeypatch.setattr("app.llm.client.anthropic.Anthropic", Broken)
    with pytest.raises(TypeError):
        AnthropicSender("k", max_retries=0).send({"model": "m"})


def test_sender_streams_text_as_it_arrives(monkeypatch: pytest.MonkeyPatch) -> None:
    final = fake_message("Hello there.")

    def delta(text: str) -> object:
        return types.SimpleNamespace(
            type="content_block_delta", delta=types.SimpleNamespace(type="text_delta", text=text)
        )

    tool_start = types.SimpleNamespace(
        type="content_block_start",
        content_block=types.SimpleNamespace(type="server_tool_use", name="web_search"),
    )

    class Stream:
        def __iter__(self) -> object:
            return iter([tool_start, delta("Hello "), delta("there.")])

        def __enter__(self) -> Stream:
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

        def get_final_message(self) -> object:
            return final

    class Streaming:
        def __init__(self, **_kwargs: object) -> None:
            self.messages = self

        def stream(self, **_params: object) -> Stream:
            return Stream()

    monkeypatch.setattr("app.llm.client.anthropic.Anthropic", Streaming)
    pieces: list[str] = []
    tools: list[str] = []
    sender = AnthropicSender("k", max_retries=0)
    assert sender.stream({"model": "m"}, pieces.append, tools.append) is final
    assert pieces == ["Hello ", "there."] and tools == ["web_search"]
