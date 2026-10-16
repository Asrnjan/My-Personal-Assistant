from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import pytest

from app.config import DEFAULT_CONTACTS_PATH, DEFAULT_POLICY_PATH
from app.db.models import ItemRow
from app.domain.delegate import Delegate
from app.domain.enums import Channel, ItemKind, ItemStatus, Source
from app.engine.approval import ApprovalPolicy, Level, load_policy
from app.engine.contacts import display_names, load_contacts
from app.engine.delegation import (
    NameResolver,
    channel_phrase,
    delegation_readback,
    reachable_channels,
    render_message,
    spoken_day,
    template_brief,
)
from app.engine.recipients import RecipientGuard
from app.integrations.fake import FakeNotifier
from app.llm.schemas import DelegationBrief
from tests.unit.llm.conftest import IST

DELEGATES = load_contacts(DEFAULT_CONTACTS_PATH)
TODAY = date(2026, 10, 16)  # a Friday


def item(**kw: object) -> ItemRow:
    values: dict[str, object] = {
        "id": "i1",
        "source": Source.JIRA,
        "source_id": "OPS-1",
        "kind": ItemKind.TASK,
        "title": "Vendor contract review",
        "description": "",
        "participants": [],
        "status": ItemStatus.OPEN,
    }
    values.update(kw)
    return ItemRow(**values)


# --- approval policy -------------------------------------------------------------------


def test_policy_levels_and_example_file() -> None:
    policy = load_policy(DEFAULT_POLICY_PATH)
    assert policy == ApprovalPolicy()  # the example file matches the defaults
    assert policy.level("delegate") == Level.CONFIRM
    assert policy.level("nudge_delegate") == Level.AUTO
    assert policy.level("payment") == Level.NEVER
    assert policy.level("something_new") == Level.CONFIRM


@pytest.mark.parametrize(
    ("utterance", "forbidden"),
    [
        ("Pay the Acme invoice", True),
        ("wire 5000 to the vendor", True),
        ("Transfer the funds for payroll", True),
        ("Buy 20 laptops", True),
        ("Approve the expense report", True),
        ("Delete all my emails", True),
        ("Issue a refund to the client", True),
        ("Delegate paying the invoice to Meera", False),
        ("Ask Meera to approve the invoices", False),
        ("Decline the 4 pm", False),
        ("Why is the budget sync high priority?", False),
    ],
)
def test_forbidden_requests(utterance: str, forbidden: bool) -> None:
    assert ApprovalPolicy().forbidden_request(utterance) is forbidden


def test_policy_validation(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="both"):
        ApprovalPolicy(auto=["delegate"])
    assert load_policy(tmp_path / "missing.yaml") == ApprovalPolicy()
    (tmp_path / "empty.yaml").write_text("")
    assert load_policy(tmp_path / "empty.yaml") == ApprovalPolicy()
    (tmp_path / "bad.yaml").write_text("- a list\n")
    with pytest.raises(ValueError, match="mapping"):
        load_policy(tmp_path / "bad.yaml")


# --- name resolution -------------------------------------------------------------------


def test_resolver_exact_alias_and_phonetic() -> None:
    r = NameResolver(DELEGATES)
    assert r.resolve("Rahul").delegate is not None
    assert r.resolve("Rahul").delegate.id == "rahul"  # type: ignore[union-attr]
    assert r.resolve("to Rahul Mehta").delegate.id == "rahul-m"  # type: ignore[union-attr]
    assert r.resolve("Mehta").delegate.id == "rahul-m"  # type: ignore[union-attr]
    assert r.resolve("Mira").delegate.id == "meera"  # type: ignore[union-attr]
    assert r.resolve("Arjoon").delegate.id == "arjun"  # type: ignore[union-attr]
    assert r.resolve("meera.shah@example.com").delegate.id == "meera"  # type: ignore[union-attr]


def test_resolver_stt_misspelling_uses_ownership_to_break_ties() -> None:
    r = NameResolver(DELEGATES)
    vendor = r.resolve("Raul", "Vendor contract review")
    assert vendor.ok and vendor.delegate is not None and vendor.delegate.id == "rahul"
    hiring = r.resolve("Raul", "Hiring plan")
    assert not hiring.ok
    assert hiring.question == "Rahul Kumar or Rahul Mehta?"
    assert [d.id for d in hiring.candidates] == ["rahul", "rahul-m"]


def test_resolver_unknowns() -> None:
    r = NameResolver(DELEGATES)
    assert r.resolve("x@evil.com").question is not None
    assert "don't have anyone called Bob" in (r.resolve("Bob").question or "")
    assert r.resolve("  ").question == "Who should I give it to?"
    trio = [
        Delegate(id=f"s{n}", full_name=f"Sam {last}", email=f"s{n}@x.com")
        for n, last in enumerate(["Lee", "Ray", "Fox"])
    ]
    assert NameResolver(trio).resolve("Sam").question == "Sam Lee, Sam Ray, or Sam Fox?"
    assert display_names(trio) == {"s0": "Sam Lee", "s1": "Sam Ray", "s2": "Sam Fox"}


# --- wording -----------------------------------------------------------------------------


def test_spoken_days_and_readbacks() -> None:
    assert spoken_day(TODAY, TODAY) == "today"
    assert spoken_day(date(2026, 10, 17), TODAY) == "tomorrow"
    assert spoken_day(date(2026, 10, 19), TODAY) == "Monday"
    assert spoken_day(date(2026, 11, 2), TODAY) == "Monday 2 November"
    assert channel_phrase([Channel.EMAIL, Channel.TELEGRAM]) == "email and Telegram"
    task = item()
    assert delegation_readback(
        "Rahul", task, date(2026, 10, 19), [Channel.EMAIL, Channel.TELEGRAM], IST, TODAY
    ) == ("Sending Rahul Vendor contract review, due Monday, by email and Telegram. Confirm?")
    assert delegation_readback("Rahul", task, None, [Channel.SLACK], IST, TODAY) == (
        "Sending Rahul Vendor contract review by Slack. Confirm?"
    )
    meeting = item(kind=ItemKind.MEETING, title="Weekly sync")
    meeting.start = datetime(2026, 10, 16, 16, 0, tzinfo=IST)
    assert delegation_readback("Meera", meeting, None, [Channel.TEAMS], IST, TODAY) == (
        "Asking Meera to cover Weekly sync at 4 pm for you by Teams, "
        "and declining it on your calendar. Confirm?"
    )


def test_reachable_channels_follow_preferences() -> None:
    rahul = next(d for d in DELEGATES if d.id == "rahul")
    notifiers = [FakeNotifier(channel=Channel.TELEGRAM), FakeNotifier(channel=Channel.SLACK)]
    assert reachable_channels(rahul, notifiers) == [Channel.TELEGRAM]


def test_template_and_render() -> None:
    brief = template_brief(
        item(reason="It renews this month."), "Rahul", "Monday", "keep_me_updated", "Loop in legal"
    )
    assert brief.task_summary == "Hi Rahul, please take over Vendor contract review. Loop in legal."
    assert brief.why_it_matters == "It renews this month."
    msg = render_message(
        DelegationBrief(
            subject="Review <Acme> terms",
            task_summary="Check the terms & conditions.",
            why_it_matters="Renewal.",
            expected_outcome="A recommendation.",
            deadline="Monday",
            context_links=[],
            handoff_type="keep_me_updated",
        ),
        ["https://jira.example.com/browse/OPS-1"],
        "Rahul",
        "abc12345",
    )
    assert msg.subject == "Delegated: Review <Acme> terms [ref abc12345]"
    assert "Deadline: Monday" in msg.text and "Please keep me posted" in msg.text
    assert "terms &amp; conditions" in msg.html and "<Acme>" not in msg.html
    assert 'href="https://jira.example.com/browse/OPS-1"' in msg.html
    bare = render_message(brief.model_copy(update={"deadline": None}), [], "Rahul", "r")
    assert "Deadline" not in bare.text and "Links" not in bare.text


def test_recipient_guard() -> None:
    guard = RecipientGuard(DELEGATES)
    meeting = item(participants=["guest@partner.com"], requester="boss@client.com")
    assert guard.allowed("RAHUL.KUMAR@example.com")
    assert guard.allowed("guest@partner.com", meeting)
    assert guard.allowed("boss@client.com", meeting)
    assert not guard.allowed("x@evil.com", meeting)
    assert not guard.allowed("guest@partner.com")
    assert not guard.allowed("")
