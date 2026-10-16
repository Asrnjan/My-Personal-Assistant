from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy.orm import Session, sessionmaker

from app.config import DEFAULT_CONTACTS_PATH, DEFAULT_RUBRIC_PATH
from app.engine.contacts import all_delegates, load_contacts, seed_delegates
from app.engine.rubric import Rubric, load_rubric


def test_example_rubric_loads_and_renders_deterministically() -> None:
    rubric = load_rubric(DEFAULT_RUBRIC_PATH)
    assert rubric.requester_tiers["board"].weight == 1.0
    assert rubric.hard_rules[0].band == "P1_do_myself"
    assert rubric.as_prompt_text() == load_rubric(DEFAULT_RUBRIC_PATH).as_prompt_text()
    assert "rules_only_thresholds" not in rubric.as_prompt_text()


def test_rubric_defaults_and_validation(tmp_path: Path) -> None:
    assert load_rubric(tmp_path / "missing.yaml") == Rubric()
    (tmp_path / "empty.yaml").write_text("")
    assert load_rubric(tmp_path / "empty.yaml") == Rubric()
    bad = tmp_path / "bad.yaml"
    for body in ("weights: {vibes: 1}\n", "weights: {cost_of_skipping: 0}\n", "- x\n"):
        bad.write_text(body)
        with pytest.raises((ValidationError, ValueError)):
            load_rubric(bad)


def test_contacts_load_and_seed(session_factory: sessionmaker[Session], tmp_path: Path) -> None:
    contacts = load_contacts(DEFAULT_CONTACTS_PATH)
    assert len(contacts) == 4
    assert {c.id for c in contacts} == {"rahul", "rahul-m", "meera", "arjun"}
    assert seed_delegates(session_factory, contacts) == 4
    assert seed_delegates(session_factory, contacts) == 0
    with session_factory() as s:
        loaded = {d.id: d for d in all_delegates(s)}
    assert loaded["rahul"].preferred_channels == ["email", "telegram"]
    assert loaded["meera"].ownership_areas[0] == "finance"

    assert load_contacts(tmp_path / "none.yaml") == []
    dupes = tmp_path / "d.yaml"
    dupes.write_text(
        "delegates:\n"
        "  - {id: a, full_name: A, email: a@example.com}\n"
        "  - {id: a, full_name: B, email: b@example.com}\n"
    )
    with pytest.raises(ValueError, match="duplicate"):
        load_contacts(dupes)
