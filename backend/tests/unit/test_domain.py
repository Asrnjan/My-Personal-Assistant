from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from app.domain.decision import Decision, FollowUp
from app.domain.delegate import Delegate
from app.domain.enums import (
    Actor,
    ApprovedVia,
    Channel,
    DecisionAction,
    FollowUpState,
    ItemKind,
    PriorityBand,
    Source,
)
from app.domain.item import ItemBase, ItemScore

NOW = datetime(2026, 10, 16, 9, 0, tzinfo=UTC)


def test_item_rejects_end_before_start() -> None:
    with pytest.raises(ValidationError, match="end must not be before start"):
        ItemBase(
            source=Source.GCAL,
            source_id="e1",
            kind=ItemKind.MEETING,
            title="Budget sync",
            start=NOW,
            end=NOW - timedelta(minutes=30),
        )


def test_item_defaults() -> None:
    item = ItemBase(source=Source.JIRA, source_id="OPS-1", kind=ItemKind.TASK, title="Review")
    assert item.participants == []
    assert item.status == "open"


@pytest.mark.parametrize(
    "overrides",
    [{"priority_score": 101}, {"confidence": 1.5}, {"reason": ""}, {"priority_band": "P9"}],
)
def test_score_bounds(overrides: dict[str, object]) -> None:
    data: dict[str, object] = {
        "priority_score": 80,
        "priority_band": PriorityBand.P1_DO_MYSELF,
        "reason": "The board asked for it.",
        "confidence": 0.9,
        "ceo_required": True,
    }
    data.update(overrides)
    with pytest.raises(ValidationError):
        ItemScore.model_validate(data)


def test_delegate_validates_email_and_channels() -> None:
    d = Delegate(id="rahul", full_name="Rahul Kumar", email="rahul@example.com")
    assert d.preferred_channels == [Channel.EMAIL]
    with pytest.raises(ValidationError):
        Delegate(id="x", full_name="X", email="not-an-email")
    with pytest.raises(ValidationError):
        Delegate.model_validate(
            {"id": "x", "full_name": "X", "email": "x@example.com", "preferred_channels": ["sms"]}
        )


def test_decision_and_followup() -> None:
    dec = Decision(
        id="d1",
        item_id="i1",
        action=DecisionAction.DELEGATE,
        actor=Actor.CEO,
        approved_via=ApprovedVia.VOICE,
        channels_used=[Channel.EMAIL, Channel.TELEGRAM],
    )
    assert dec.error is None
    fu = FollowUp(id="f1", decision_id=dec.id)
    assert fu.state == FollowUpState.PENDING
    with pytest.raises(ValidationError):
        FollowUp(id="f2", decision_id="d1", nudges_sent=-1)
