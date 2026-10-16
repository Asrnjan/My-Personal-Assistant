"""Asana adapter (REST API 1.0, personal access token)."""

from __future__ import annotations

from typing import Any

from app.domain.enums import Source
from app.integrations.base import ExternalTask, TaskAdapter
from app.integrations.http import HttpClient
from app.integrations.mime import parse_date, parse_iso

BASE_URL = "https://app.asana.com/api/1.0"
_FIELDS = "name,notes,due_on,due_at,permalink_url,completed,assignee.email,created_by.email"


class AsanaAdapter(TaskAdapter):
    source = Source.ASANA

    def __init__(self, http: HttpClient, workspace_gid: str) -> None:
        self._http = http
        self._workspace = workspace_gid

    def list_open(self) -> list[ExternalTask]:
        tasks: list[ExternalTask] = []
        params: dict[str, str] = {
            "assignee": "me",
            "workspace": self._workspace,
            "completed_since": "now",  # only incomplete tasks
            "opt_fields": _FIELDS,
            "limit": "100",
        }
        while True:
            body = self._http.get_json("/tasks", params=params)
            tasks.extend(_task(t) for t in body.get("data", []) if not t.get("completed"))
            offset = (body.get("next_page") or {}).get("offset")
            if not offset:
                return tasks
            params = {**params, "offset": offset}

    def reassign(self, task_id: str, platform_user_id: str) -> None:
        self._http.send_json("PUT", f"/tasks/{task_id}", {"data": {"assignee": platform_user_id}})

    def comment(self, task_id: str, text: str) -> None:
        self._http.send_json("POST", f"/tasks/{task_id}/stories", {"data": {"text": text}})

    def close(self, task_id: str) -> None:
        self._http.send_json("PUT", f"/tasks/{task_id}", {"data": {"completed": True}})


def _task(t: dict[str, Any]) -> ExternalTask:
    return ExternalTask(
        source=Source.ASANA,
        id=t["gid"],
        title=t.get("name") or "(untitled)",
        description=t.get("notes", ""),
        url=t.get("permalink_url"),
        assignee=(t.get("assignee") or {}).get("email"),
        reporter=(t.get("created_by") or {}).get("email"),
        due=parse_iso(t.get("due_at")) or parse_date(t.get("due_on")),
        status="completed" if t.get("completed") else "open",
        done=bool(t.get("completed")),
    )
