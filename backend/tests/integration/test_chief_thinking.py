"""Thinking partner: memory, decisions grounded in the CEO's context, drafts in their voice."""

from __future__ import annotations

from datetime import timedelta

from app.domain.enums import Source
from app.integrations.base import CalendarEvent, EmailMessage
from app.llm.fake import fake_message
from app.scheduler import AssistantJobs
from tests.harness import Harness, at_ist, harness, say, tool_call  # noqa: F401


def _system(h: Harness) -> str:
    return " ".join(b["text"] for b in h.sender.calls[-1]["system"])


def test_remembered_facts_are_used_everywhere(harness: Harness) -> None:
    harness.sender.queue(tool_call("remember", fact="Acme's renewal is in March"), say("Noted."))
    harness.converse("Remember that Acme's renewal is in March")
    memory = harness.app.state.agent.skills.memory
    assert memory.all() == ["Acme's renewal is in March."]

    # In every conversation turn's system prompt...
    harness.sender.queue(say("Sure."))
    harness.converse("What's up?")
    assert "Things the CEO asked you to remember:\nAcme's renewal is in March." in _system(harness)
    # ...when asked...
    harness.sender.queue(tool_call("recall", about="Acme"))
    harness.converse("What do I know about Acme?")
    assert (
        "renewal is in March" in harness.sender.calls[-1]["messages"][-1]["content"][0]["content"]
    )

    # ...and in meeting prep.
    start = harness.clock() + timedelta(minutes=5)
    harness.calendar.events = {
        "m": CalendarEvent(Source.GCAL, "m", "Acme renewal call", start, start + timedelta(hours=1))
    }
    harness.sync()
    jobs: AssistantJobs = harness.app.state.jobs
    jobs.send_preps()
    assert "Acme's renewal is in March." in harness.sender.calls[-1]["messages"][0]["content"]

    harness.sender.queue(tool_call("forget", fact="Acme renewal"))
    harness.converse("Forget the Acme renewal")
    assert memory.all() == []


def test_think_through_uses_context_and_more_effort(harness: Harness) -> None:
    harness.mail.inbox = [
        EmailMessage(
            Source.GMAIL,
            "g-1",
            "t-1",
            "Sales hiring",
            "cfo@acme.com",
            snippet="Budget for one more sales lead is tight.",
            received_at=at_ist(8),
        )
    ]
    harness.app.state.agent.skills.memory.remember("Sales target doubled this year")
    harness.sender.queue(
        tool_call("think_through", question="Should I hire a second sales lead?"),
        fake_message("Hire, if the budget allows. Ask the CFO first."),
    )
    harness.converse("Should I hire a second sales lead?")
    decision = harness.sender.calls[-2]
    assert decision["output_config"]["effort"] == "high"
    prompt = decision["messages"][0]["content"]
    assert "Sales target doubled this year." in prompt
    assert "Budget for one more sales lead is tight." in prompt
    result = harness.sender.calls[-1]["messages"][-1]["content"][0]["content"]
    assert "Ask the CFO first." in result


def test_writing_style_is_learned_and_used_for_drafts(harness: Harness) -> None:
    harness.mail.outbox = [
        EmailMessage(
            Source.GMAIL,
            "s-1",
            "t-1",
            "Update",
            "ceo@example.com",
            snippet="Hi all, quick update. Thanks, A",
            received_at=at_ist(8),
        )
    ]
    jobs: AssistantJobs = harness.app.state.jobs
    jobs.read_sent_mail()
    thinking = harness.app.state.agent.skills.thinking
    assert thinking.style() == "Short and direct. Opens with Hi, signs off Thanks."

    harness.sender.queue(
        tool_call("write_draft", kind="LinkedIn post", brief="We opened a Pune office"),
    )
    harness.converse("Draft a LinkedIn post about our Pune office")
    writer = harness.sender.calls[-2]
    assert "Opens with Hi" in " ".join(b["text"] for b in writer["system"])
    assert '"draft": "[draft]"' in harness.sender.calls[-1]["messages"][-1]["content"][0]["content"]
    # Conversation drafting uses the style too.
    assert "How the CEO writes" in _system(harness)


def test_nothing_to_learn_without_sent_mail(harness: Harness) -> None:
    assert harness.app.state.agent.skills.thinking.learn_style() == ""
