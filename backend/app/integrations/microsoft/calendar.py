"""Outlook calendar adapter (Microsoft Graph)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.domain.enums import Source
from app.integrations.base import Attendee, CalendarAdapter, CalendarEvent, NewEvent, SyncResult
from app.integrations.http import HttpClient, IntegrationError
from app.integrations.microsoft.graph import UTC_PREFER, iterate_pages
from app.integrations.mime import parse_iso

_GRAPH_RESPONSE = {
    "accepted": "accepted",
    "declined": "declined",
    "tentativelyAccepted": "tentative",
    "notResponded": "needsAction",
    "none": "needsAction",
    "organizer": "accepted",
}


class OutlookCalendarAdapter(CalendarAdapter):
    source = Source.OUTLOOK

    def __init__(self, http: HttpClient) -> None:
        self._http = http

    def sync_events(
        self, start: datetime, end: datetime, cursor: str | None = None
    ) -> SyncResult[CalendarEvent]:
        if cursor:
            try:
                return self._delta(cursor, None, full=False)
            except IntegrationError as exc:
                if exc.status != 410:  # delta token expired: start over
                    raise
        params = {
            "startDateTime": start.astimezone(UTC).isoformat(),
            "endDateTime": end.astimezone(UTC).isoformat(),
        }
        return self._delta("/me/calendarView/delta", params, full=True)

    def _delta(
        self, url: str, params: dict[str, str] | None, *, full: bool
    ) -> SyncResult[CalendarEvent]:
        result: SyncResult[CalendarEvent] = SyncResult(full=full)
        prefer = f"{UTC_PREFER}, odata.maxpagesize=100"
        for page in iterate_pages(self._http, url, params, prefer=prefer):
            result.records.extend(_event(e) for e in page.get("value", []))
            if "@odata.deltaLink" in page:
                result.cursor = page["@odata.deltaLink"]
        return result

    def decline(self, event_id: str, note: str) -> None:
        self._http.send_json(
            "POST", f"/me/events/{event_id}/decline", {"comment": note, "sendResponse": True}
        )

    def add_attendee(self, event_id: str, email: str) -> None:
        event = self._http.get_json(f"/me/events/{event_id}", params={"$select": "attendees"})
        attendees: list[dict[str, Any]] = event.get("attendees", [])
        if any(a["emailAddress"]["address"].lower() == email.lower() for a in attendees):
            return
        attendees.append({"emailAddress": {"address": email}, "type": "required"})
        self._http.send_json("PATCH", f"/me/events/{event_id}", {"attendees": attendees})

    def create_event(self, event: NewEvent) -> CalendarEvent:
        body: dict[str, Any] = {
            "subject": event.title,
            "body": {"contentType": "Text", "content": event.description},
            "start": {"dateTime": _graph_time(event.start), "timeZone": "UTC"},
            "end": {"dateTime": _graph_time(event.end), "timeZone": "UTC"},
            "attendees": [
                {"emailAddress": {"address": a}, "type": "required"} for a in event.attendees
            ],
        }
        if event.online:
            body["isOnlineMeeting"] = True
            body["onlineMeetingProvider"] = "teamsForBusiness"
        created = self._http.send_json("POST", "/me/events", body, headers={"Prefer": UTC_PREFER})
        return _event(created)

    def move_event(self, event_id: str, start: datetime, end: datetime) -> None:
        self._http.send_json(
            "PATCH",
            f"/me/events/{event_id}",
            {
                "start": {"dateTime": _graph_time(start), "timeZone": "UTC"},
                "end": {"dateTime": _graph_time(end), "timeZone": "UTC"},
            },
        )


def _graph_time(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S")


def _event(e: dict[str, Any]) -> CalendarEvent:
    if "@removed" in e:
        return CalendarEvent(
            source=Source.OUTLOOK, id=e["id"], title="", start=None, end=None, cancelled=True
        )
    organizer = ((e.get("organizer") or {}).get("emailAddress") or {}).get("address")
    return CalendarEvent(
        source=Source.OUTLOOK,
        id=e["id"],
        title=e.get("subject") or "(no title)",
        start=parse_iso((e.get("start") or {}).get("dateTime")),
        end=parse_iso((e.get("end") or {}).get("dateTime")),
        organizer=organizer,
        attendees=tuple(
            Attendee(
                email=a["emailAddress"].get("address", ""),
                name=a["emailAddress"].get("name"),
                response=_GRAPH_RESPONSE.get((a.get("status") or {}).get("response", "none")),
            )
            for a in e.get("attendees", [])
        ),
        description=e.get("bodyPreview", ""),
        url=e.get("webLink"),
        all_day=bool(e.get("isAllDay")),
        cancelled=bool(e.get("isCancelled")),
        ical_uid=e.get("iCalUId"),
    )
