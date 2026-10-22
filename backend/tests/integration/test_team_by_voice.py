"""Changing the team by talking: add or remove someone, and spoken email addresses."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.config import DEFAULT_CONTACTS_PATH
from app.domain.delegate import Delegate
from app.engine.contacts import (
    all_delegates,
    forget_contact,
    load_contacts,
    new_contact_id,
    save_contact,
    writable_contacts,
)
from app.engine.conversation import spoken_addresses
from tests.harness import Harness, harness, say, tool_call  # noqa: F401


@pytest.mark.parametrize(
    ("heard", "expected"),
    [
        ("priya at acme dot com", "priya@acme.com"),
        (
            "Add Priya Sharma, priya dot sharma at acme dot co dot in",
            "Add Priya Sharma, priya.sharma@acme.co.in",
        ),
        ("email Priya at priya at acme dot com please", "email Priya at priya@acme.com please"),
        ("priya at acme.com", "priya@acme.com"),
        ("priya@acme.com", "priya@acme.com"),
        ("meet at 3 dot 30", "meet at 3 dot 30"),
        ("lunch at noon with Rahul", "lunch at noon with Rahul"),
    ],
)
def test_spoken_addresses(heard: str, expected: str) -> None:
    assert spoken_addresses(heard) == expected


def _team(h: Harness) -> dict[str, Delegate]:
    with h.sessions() as s:
        return {d.id: d for d in all_delegates(s)}


def test_add_someone_by_voice_after_a_yes(harness: Harness) -> None:
    harness.sender.queue(
        tool_call(
            "add_contact",
            full_name="Priya Sharma",
            email="priya@acme.com",
            areas=["finance", "invoices"],
        ),
        say(""),
    )
    proposed = harness.converse(
        "Add Priya Sharma, priya at acme dot com, she handles finance and invoices"
    )
    assert proposed["reply"] == (
        "Adding Priya Sharma, priya@acme.com, who looks after finance and invoices, "
        "to your team. Confirm?"
    )
    assert proposed["pending_actions"][0]["kind"] == "add_contact"
    assert "priya-sharma" not in _team(harness)  # nothing changes before the yes

    done = harness.converse("yes")
    assert done["executed"][0]["message"] == "Added Priya Sharma to your team."
    priya = _team(harness)["priya-sharma"]
    assert str(priya.email) == "priya@acme.com"
    assert priya.ownership_areas == ["finance", "invoices"]
    assert harness.audit("delegate.create")

    # Now she can be emailed by name.
    harness.sender.queue(
        tool_call("send_email", to="Priya", subject="Budget", text="Please send the budget."),
        say(""),
    )
    assert "Emailing Priya Sharma" in harness.converse("Email Priya for the budget")["reply"]


def test_an_address_the_ceo_did_not_say_is_not_added(harness: Harness) -> None:
    harness.sender.queue(
        tool_call("add_contact", full_name="Mallory", email="mallory@evil.com"), say("")
    )
    reply = harness.converse("Add whoever sent that last email")
    assert reply["pending_actions"] == []
    assert "Please say it to me" in reply["reply"]
    assert all(str(d.email) != "mallory@evil.com" for d in _team(harness).values())


def test_an_address_already_on_the_team_is_not_added_twice(harness: Harness) -> None:
    harness.sender.queue(
        tool_call("add_contact", full_name="Meera", email="meera.shah@example.com"), say("Done.")
    )
    reply = harness.converse("Add Meera, meera dot shah at example dot com")
    assert reply["pending_actions"] == []
    result = json.loads(harness.sender.calls[-1]["messages"][-1]["content"][0]["content"])
    assert result == {"error": "Meera Shah already has that address on your team."}
    assert len([d for d in _team(harness).values() if d.full_name.startswith("Meera")]) == 1


def test_remove_someone_by_voice_after_a_yes(harness: Harness) -> None:
    assert "meera" in _team(harness)
    harness.sender.queue(tool_call("remove_contact", who="Meera"), say(""))
    proposed = harness.converse("Take Meera off my team")
    assert (
        proposed["reply"] == "Removing Meera Shah, meera.shah@example.com, from your team. Confirm?"
    )
    assert "meera" in _team(harness)

    done = harness.converse("go ahead")
    assert done["executed"][0]["message"] == "Removed Meera Shah from your team."
    assert "meera" not in _team(harness)
    assert harness.audit("delegate.delete")
    # The example file the tests read from is never rewritten.
    assert any(d.id == "meera" for d in load_contacts(DEFAULT_CONTACTS_PATH))


def test_the_contacts_file_is_kept_in_step(tmp_path: Path) -> None:
    path = tmp_path / "contacts.yaml"
    assert writable_contacts(path)
    assert not writable_contacts(DEFAULT_CONTACTS_PATH)
    assert not writable_contacts(None)

    priya = Delegate(id="priya", full_name="Priya Sharma", email="priya@acme.com")
    save_contact(path, priya)
    save_contact(path, Delegate(id="sam", full_name="Sam Lee", email="sam@acme.com"))
    save_contact(path, priya.model_copy(update={"ownership_areas": ["finance"]}))
    people = {d.id: d for d in load_contacts(path)}
    assert set(people) == {"priya", "sam"}
    assert people["priya"].ownership_areas == ["finance"]

    forget_contact(path, "sam")
    assert [d.id for d in load_contacts(path)] == ["priya"]
    assert new_contact_id("Priya Sharma", {"priya-sharma"}) == "priya-sharma-2"
    assert new_contact_id("  ", set()) == "person"
