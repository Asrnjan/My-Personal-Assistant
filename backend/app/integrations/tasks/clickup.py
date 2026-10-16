"""ClickUp adapter (API v2, personal token)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.domain.enums import Source
from app.integrations.base import ExternalTask, TaskAdapter
from app.integrations.http import HttpClient

BASE_URL = "https://api.clickup.com/api/v2"


class ClickUpAdapter(TaskAdapter):
    source = Source.CLICKUP

    def __init__(
        self, http: HttpClient, team_id: str, user_id: str, done_status: str = "complete"
    ) -> None:
        self._http = http
        self._team = team_id
        self._user = user_id
        self._done_status = done_status

    def list_open(self) -> list[ExternalTask]:
        tasks: list[ExternalTask] = []
        page = 0
        while True:
            body = self._http.get_json(
                f"/team/{self._team}/task",
                params={
                    "assignees[]": self._user,
                    "include_closed": "false",
                    "subtasks": "true",
                    "page": str(page),
                },
            )
            batch = body.get("tasks", [])
            tasks.extend(_task(t) for t in batch)
            if body.get("last_page", True) or not batch:
                return tasks
            page += 1

    def reassign(self, task_id: str, platform_user_id: str) -> None:
        current = self._http.get_json(f"/task/{task_id}")
        existing = [a["id"] for a in current.get("assignees", [])]
        new_id = int(platform_user_id)
        self._http.send_json(
            "PUT",
            f"/task/{task_id}",
            {"assignees": {"add": [new_id], "rem": [i for i in existing if i != new_id]}},
        )

    def comment(self, task_id: str, text: str) -> None:
        self._http.send_json(
            "POST", f"/task/{task_id}/comment", {"comment_text": text, "notify_all": True}
        )

    def close(self, task_id: str) -> None:
        self._http.send_json("PUT", f"/task/{task_id}", {"status": self._done_status})


def _task(t: dict[str, Any]) -> ExternalTask:
    status = t.get("status") or {}
    due_ms = t.get("due_date")
    assignees = t.get("assignees") or []
    return ExternalTask(
        source=Source.CLICKUP,
        id=t["id"],
        title=t.get("name") or "(untitled)",
        description=t.get("text_content") or t.get("description") or "",
        url=t.get("url"),
        assignee=assignees[0].get("email") if assignees else None,
        reporter=(t.get("creator") or {}).get("email"),
        due=datetime.fromtimestamp(int(due_ms) / 1000, UTC) if due_ms else None,
        status=status.get("status", ""),
        done=status.get("type") in ("closed", "done"),
    )
