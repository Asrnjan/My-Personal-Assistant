"""Google Calendar adapter (Calendar API v3)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from app.domain.enums import Source
from app.integrations.base import Attendee, CalendarAdapter, CalendarEvent, NewEvent, SyncResult
from app.integrations.http import HttpClient, IntegrationError
from app.integrations.mime import parse_date, parse_iso

BASE_URL = "https://www.googleapis.com/calendar/v3"


class GoogleCalendarAdapter(CalendarAdapter):
    source = Source.GCAL

    def __init__(self, http: HttpClient, calendar_id: str = "primary") -> None:
        self._http = http
        self._events_url = f"/calendars/{calendar_id}/events"

    def sync_events(
        self, start: datetime, end: datetime, cursor: str | None = None
    ) -> SyncResult[CalendarEvent]:
        if cursor:
            try:
                return self._pages({"syncToken": cursor}, full=False)
            except IntegrationError as exc:
                if exc.status != 410:  # 410 Gone: sync token expired, do a full sync
                    raise
        params = {
            "timeMin": start.isoformat(),
            "timeMax": end.isoformat(),
            "singleEvents": "true",
        }
        return self._pages(params, full=True)

    def _pages(self, params: dict[str, str], *, full: bool) -> SyncResult[CalendarEvent]:
        result: SyncResult[CalendarEvent] = SyncResult(full=full)
        page_token: str | None = None
        while True:
            query = {**params, "maxResults": "250"}
            if page_token:
                query["pageToken"] = page_token
            body = self._http.get_json(self._events_url, params=query)
            result.records.extend(_event(e) for e in body.get("items", []))
            page_token = body.get("nextPageToken")
            if not page_token:
                result.cursor = body.get("nextSyncToken")
                return result

    def decline(self, event_id: str, note: str) -> None:
        event = self._http.get_json(f"{self._events_url}/{event_id}")
        attendees = event.get("attendees", [])
        me = next((a for a in attendees if a.get("self")), None)
        if me is None:
            raise IntegrationError("google", "the CEO is not an attendee of this event")
        me["responseStatus"] = "declined"
        me["comment"] = note
        self._patch_attendees(event_id, attendees)

    def add_attendee(self, event_id: str, email: str) -> None:
        event = self._http.get_json(f"{self._events_url}/{event_id}")
        attendees = event.get("attendees", [])
        if not any(a.get("email", "").lower() == email.lower() for a in attendees):
            attendees.append({"email": email})
            self._patch_attendees(event_id, attendees)

    def create_event(self, event: NewEvent) -> CalendarEvent:
        body: dict[str, Any] = {
            "summary": event.title,
            "description": event.description,
            "start": {"dateTime": event.start.isoformat()},
            "end": {"dateTime": event.end.isoformat()},
            "attendees": [{"email": a} for a in event.attendees],
        }
        params = {"sendUpdates": "all"}
        if event.online:
            body["conferenceData"] = {
                "createRequest": {
                    "requestId": uuid.uuid4().hex,
                    "conferenceSolutionKey": {"type": "hangoutsMeet"},
                }
            }
            params["conferenceDataVersion"] = "1"
        created = self._http.send_json("POST", self._events_url, body, params=params)
        return _event(created)

    def _patch_attendees(self, event_id: str, attendees: list[dict[str, Any]]) -> None:
        self._http.send_json(
            "PATCH",
            f"{self._events_url}/{event_id}",
            {"attendees": attendees},
            params={"sendUpdates": "all"},
        )


def _event(e: dict[str, Any]) -> CalendarEvent:
    start, end = e.get("start", {}), e.get("end", {})
    all_day = "date" in start and "dateTime" not in start
    return CalendarEvent(
        source=Source.GCAL,
        id=e["id"],
        title=e.get("summary", "(no title)"),
        start=parse_date(start.get("date")) if all_day else parse_iso(start.get("dateTime")),
        end=parse_date(end.get("date")) if all_day else parse_iso(end.get("dateTime")),
        organizer=(e.get("organizer") or {}).get("email"),
        attendees=tuple(
            Attendee(
                email=a.get("email", ""),
                name=a.get("displayName"),
                response=a.get("responseStatus"),
                is_self=bool(a.get("self")),
            )
            for a in e.get("attendees", [])
        ),
        description=e.get("description", ""),
        url=e.get("htmlLink"),
        all_day=all_day,
        cancelled=e.get("status") == "cancelled",
        ical_uid=e.get("iCalUID"),
    )
