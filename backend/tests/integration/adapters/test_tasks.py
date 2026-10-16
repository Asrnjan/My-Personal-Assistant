from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime

import httpx
import pytest
import respx

from app.integrations.http import HttpClient, IntegrationError
from app.integrations.tasks.asana import BASE_URL as ASANA
from app.integrations.tasks.asana import AsanaAdapter
from app.integrations.tasks.clickup import BASE_URL as CLICKUP
from app.integrations.tasks.clickup import ClickUpAdapter
from app.integrations.tasks.jira import (
    JiraAdapter,
    adf_to_text,
    basic_auth_header,
    text_to_adf,
)
from tests.integration.adapters.conftest import fixture

JIRA = "https://acme.atlassian.net"


@pytest.fixture
def jira(make_http: Callable[..., HttpClient]) -> JiraAdapter:
    headers = basic_auth_header("ceo@example.com", "tok")
    return JiraAdapter(make_http("jira", JIRA, auth_headers=lambda: headers), JIRA)


def test_jira_list_open_paginates(mock_http: respx.MockRouter, jira: JiraAdapter) -> None:
    search = mock_http.post(f"{JIRA}/rest/api/3/search/jql").mock(
        side_effect=[
            httpx.Response(200, json=fixture("jira/search_page1.json")),
            httpx.Response(200, json=fixture("jira/search_page2.json")),
        ]
    )
    first, second = jira.list_open()
    assert first.id == "OPS-12"
    assert first.url == f"{JIRA}/browse/OPS-12"
    assert first.description == "Review the renewal terms.\nFlag anything over 10%."
    assert first.due == datetime(2026, 10, 19, tzinfo=UTC)
    assert first.reporter == "meera@example.com" and not first.done
    assert second.assignee is None and second.description == ""
    assert json.loads(search.calls[1].request.content)["nextPageToken"] == "tok-2"
    assert search.calls[0].request.headers["Authorization"].startswith("Basic ")


def test_jira_reassign_comment_close(mock_http: respx.MockRouter, jira: JiraAdapter) -> None:
    assign = mock_http.put(f"{JIRA}/rest/api/3/issue/OPS-12/assignee").mock(
        return_value=httpx.Response(204)
    )
    comment = mock_http.post(f"{JIRA}/rest/api/3/issue/OPS-12/comment").mock(
        return_value=httpx.Response(201, json={"id": "c1"})
    )
    mock_http.get(f"{JIRA}/rest/api/3/issue/OPS-12/transitions").mock(
        return_value=httpx.Response(200, json=fixture("jira/transitions.json"))
    )
    transition = mock_http.post(f"{JIRA}/rest/api/3/issue/OPS-12/transitions").mock(
        return_value=httpx.Response(204)
    )
    jira.reassign("OPS-12", "acc-rahul")
    jira.comment("OPS-12", "Delegated to Rahul.\n\nDue Monday.")
    jira.close("OPS-12")
    assert json.loads(assign.calls[0].request.content) == {"accountId": "acc-rahul"}
    body = json.loads(comment.calls[0].request.content)["body"]
    assert adf_to_text(body) == "Delegated to Rahul.\nDue Monday."
    assert json.loads(transition.calls[0].request.content) == {"transition": {"id": "31"}}


def test_jira_close_without_done_transition(mock_http: respx.MockRouter, jira: JiraAdapter) -> None:
    mock_http.get(f"{JIRA}/rest/api/3/issue/X-1/transitions").mock(
        return_value=httpx.Response(200, json={"transitions": []})
    )
    with pytest.raises(IntegrationError, match="no transition"):
        jira.close("X-1")


def test_adf_helpers() -> None:
    assert adf_to_text(None) == "" and adf_to_text("plain") == "plain"
    assert text_to_adf("a\n\nb")["content"][1]["content"][0]["text"] == "b"


def test_asana(mock_http: respx.MockRouter, make_http: Callable[..., HttpClient]) -> None:
    adapter = AsanaAdapter(make_http("asana", ASANA), "ws1")
    listing = mock_http.get(f"{ASANA}/tasks").mock(
        side_effect=[
            httpx.Response(200, json=fixture("asana/tasks_page1.json")),
            httpx.Response(200, json=fixture("asana/tasks_page2.json")),
        ]
    )
    first, second = adapter.list_open()
    assert first.due == datetime(2026, 10, 20, tzinfo=UTC)
    assert second.due == datetime(2026, 10, 21, 9, 0, tzinfo=UTC)
    assert first.reporter == "chief@example.com"
    params = listing.calls[1].request.url.params
    assert params["offset"] == "off-2" and params["workspace"] == "ws1"

    put = mock_http.put(f"{ASANA}/tasks/120001").mock(return_value=httpx.Response(200, json={}))
    story = mock_http.post(f"{ASANA}/tasks/120001/stories").mock(
        return_value=httpx.Response(201, json={})
    )
    adapter.reassign("120001", "999")
    adapter.comment("120001", "brief")
    adapter.close("120001")
    assert json.loads(put.calls[0].request.content) == {"data": {"assignee": "999"}}
    assert json.loads(put.calls[1].request.content) == {"data": {"completed": True}}
    assert json.loads(story.calls[0].request.content) == {"data": {"text": "brief"}}


def test_clickup(mock_http: respx.MockRouter, make_http: Callable[..., HttpClient]) -> None:
    adapter = ClickUpAdapter(make_http("clickup", CLICKUP), "team1", "501", "done")
    listing = mock_http.get(f"{CLICKUP}/team/team1/task").mock(
        side_effect=[
            httpx.Response(200, json=fixture("clickup/tasks_page0.json")),
            httpx.Response(200, json=fixture("clickup/tasks_page1.json")),
        ]
    )
    first, second = adapter.list_open()
    assert first.description == "Q4 campaign"
    assert first.due == datetime.fromtimestamp(1792195200, UTC)
    assert first.assignee == "ceo@example.com" and not first.done
    assert second.done and second.due is None
    assert listing.calls[1].request.url.params["page"] == "1"

    mock_http.get(f"{CLICKUP}/task/86a1").mock(
        return_value=httpx.Response(200, json={"assignees": [{"id": 501}, {"id": 777}]})
    )
    put = mock_http.put(f"{CLICKUP}/task/86a1").mock(return_value=httpx.Response(200, json={}))
    comment = mock_http.post(f"{CLICKUP}/task/86a1/comment").mock(
        return_value=httpx.Response(200, json={})
    )
    adapter.reassign("86a1", "777")
    adapter.comment("86a1", "brief")
    adapter.close("86a1")
    assert json.loads(put.calls[0].request.content) == {"assignees": {"add": [777], "rem": [501]}}
    assert json.loads(put.calls[1].request.content) == {"status": "done"}
    assert json.loads(comment.calls[0].request.content)["comment_text"] == "brief"
