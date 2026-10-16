"""Jira Cloud adapter (REST API v3, basic auth with an API token)."""

from __future__ import annotations

import base64
from typing import Any

from app.domain.enums import Source
from app.integrations.base import ExternalTask, TaskAdapter
from app.integrations.http import HttpClient, IntegrationError
from app.integrations.mime import parse_date

OPEN_TASKS_JQL = (
    "(assignee = currentUser() OR reporter = currentUser()) "
    "AND statusCategory != Done ORDER BY duedate ASC"
)
_FIELDS = ["summary", "description", "duedate", "status", "assignee", "reporter"]


def basic_auth_header(email: str, token: str) -> dict[str, str]:
    raw = base64.b64encode(f"{email}:{token}".encode()).decode()
    return {"Authorization": f"Basic {raw}", "Accept": "application/json"}


def adf_to_text(node: Any) -> str:
    """Flatten Atlassian Document Format to plain text."""
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if node.get("type") == "text":
        text: str = node.get("text", "")
        return text
    parts = [adf_to_text(child) for child in node.get("content", [])]
    sep = "\n" if node.get("type") in ("doc", "bulletList", "orderedList") else ""
    return sep.join(p for p in parts if p)


def text_to_adf(text: str) -> dict[str, Any]:
    return {
        "type": "doc",
        "version": 1,
        "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": line}]}
            for line in text.splitlines()
            if line.strip()
        ],
    }


class JiraAdapter(TaskAdapter):
    source = Source.JIRA

    def __init__(self, http: HttpClient, base_url: str, jql: str = OPEN_TASKS_JQL) -> None:
        self._http = http
        self._browse = base_url.rstrip("/") + "/browse/"
        self._jql = jql

    def list_open(self) -> list[ExternalTask]:
        tasks: list[ExternalTask] = []
        next_token: str | None = None
        while True:
            body: dict[str, Any] = {"jql": self._jql, "fields": _FIELDS, "maxResults": 100}
            if next_token:
                body["nextPageToken"] = next_token
            page = self._http.send_json("POST", "/rest/api/3/search/jql", body)
            tasks.extend(self._task(i) for i in page.get("issues", []))
            next_token = page.get("nextPageToken")
            if not next_token or page.get("isLast", False):
                return tasks

    def _task(self, issue: dict[str, Any]) -> ExternalTask:
        f = issue.get("fields", {})
        status = f.get("status") or {}
        return ExternalTask(
            source=Source.JIRA,
            id=issue["key"],
            title=f.get("summary") or issue["key"],
            description=adf_to_text(f.get("description")),
            url=self._browse + issue["key"],
            assignee=(f.get("assignee") or {}).get("emailAddress"),
            reporter=(f.get("reporter") or {}).get("emailAddress"),
            due=parse_date(f.get("duedate")),
            status=status.get("name", ""),
            done=(status.get("statusCategory") or {}).get("key") == "done",
        )

    def reassign(self, task_id: str, platform_user_id: str) -> None:
        self._http.send_json(
            "PUT", f"/rest/api/3/issue/{task_id}/assignee", {"accountId": platform_user_id}
        )

    def comment(self, task_id: str, text: str) -> None:
        self._http.send_json(
            "POST", f"/rest/api/3/issue/{task_id}/comment", {"body": text_to_adf(text)}
        )

    def close(self, task_id: str) -> None:
        body = self._http.get_json(f"/rest/api/3/issue/{task_id}/transitions")
        for t in body.get("transitions", []):
            if ((t.get("to") or {}).get("statusCategory") or {}).get("key") == "done":
                self._http.send_json(
                    "POST",
                    f"/rest/api/3/issue/{task_id}/transitions",
                    {"transition": {"id": t["id"]}},
                )
                return
        raise IntegrationError("jira", f"{task_id} has no transition to a Done status")
