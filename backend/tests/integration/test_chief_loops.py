"""Closing loops: chasing delegated work, the weekly team pulse, what the CEO waits on."""

from __future__ import annotations

from datetime import timedelta

from app.domain.enums import FollowUpState, ItemStatus
from app.engine.followup import FollowUpService
from app.engine.pulse import TeamPulse
from app.scheduler import AssistantJobs
from tests.harness import Harness, harness, tool_call  # noqa: F401
from tests.integration.test_delegation_flow import delegate, followups, seed


def _pulse(h: Harness) -> TeamPulse:
    found = h.app.state.agent.skills.pulse
    assert found is not None
    return found


def _state(h: Harness, fu_id: str) -> FollowUpState:
    service: FollowUpService = followups(h)
    return next(v.state for v in service.views() if v.id == fu_id)


def test_undated_work_is_chased_twice_then_left_to_the_ceo(harness: Harness) -> None:
    seed(harness)
    harness.update_item("Vendor contract review", due=None)
    fu = delegate(harness, "Vendor contract review", "rahul")
    before = len(harness.telegram.nudges)
    assert followups(harness).renudge() == 0  # too soon
    harness.clock.now += timedelta(hours=49)
    assert followups(harness).renudge() == 1
    assert "just checking you saw" in harness.telegram.nudges[-1][1]
    assert _state(harness, fu) == FollowUpState.NUDGED
    assert followups(harness).renudge() == 0  # not again so soon
    harness.clock.now += timedelta(hours=49)
    assert followups(harness).renudge() == 1
    harness.clock.now += timedelta(hours=49)
    assert followups(harness).renudge() == 0  # two is enough
    assert len(harness.telegram.nudges) == before + 2
    assert harness.telegram.ceo_messages == []  # the CEO isn't bothered


def test_accepted_work_gets_a_check_in_before_and_after_its_deadline(harness: Harness) -> None:
    seed(harness)
    fu = delegate(harness, "Hiring plan", "rahul", due="2026-10-18")
    assert followups(harness).acknowledge(fu, "test")
    assert followups(harness).check_in() == 0
    harness.clock.now += timedelta(days=1, hours=12)  # within a day of the deadline
    assert followups(harness).check_in() == 1
    assert 'how is "Hiring plan" going? It\'s due tomorrow' in harness.telegram.nudges[-1][1]
    assert followups(harness).check_in() == 0
    harness.clock.now += timedelta(days=2)
    assert followups(harness).check_in() == 1
    assert "was due" in harness.telegram.nudges[-1][1]
    assert harness.telegram.ceo_messages[-1] == (
        "Hiring plan from Rahul Kumar is past its deadline; I've asked for an update."
    )
    assert followups(harness).check_in() == 0


def test_waiting_on_pulse_and_finishing_work(harness: Harness) -> None:
    seed(harness)
    delegate(harness, "Vendor contract review", "rahul", due="2026-10-15")
    delegate(harness, "Hiring plan", "meera", due="2026-10-20")
    assert _pulse(harness).waiting_on() == [
        "Vendor contract review from Rahul Kumar, 1 day late",
        "Hiring plan from Meera, due Tuesday, not confirmed",
    ]
    harness.sender.queue(tool_call("waiting_on"))
    harness.converse("What am I waiting on?")
    assert "1 day late" in harness.sender.calls[-1]["messages"][-1]["content"][0]["content"]

    item = harness.item("Hiring plan")
    harness.sender.queue(tool_call("delegation_done", item_ref=item.id))
    harness.converse("Meera finished the hiring plan")
    assert harness.item("Hiring plan").status == ItemStatus.DONE

    jobs: AssistantJobs = harness.app.state.jobs
    pulse, activity = jobs.weekly()
    assert pulse == (
        "Team pulse for the week. Rahul Kumar is late on Vendor contract review. "
        "Meera finished 1 item."
    )
    assert harness.telegram.ceo_messages[-2:] == [pulse, activity]
    assert activity.startswith("This week I delegated 2 items.")


def test_quiet_week(harness: Harness) -> None:
    assert _pulse(harness).weekly() == "Team pulse: nothing was delegated this week."
    assert _pulse(harness).waiting_on() == []
