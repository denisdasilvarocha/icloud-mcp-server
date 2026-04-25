"""CalDAV adapter for iCloud Calendar."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any
from urllib.parse import urljoin
from xml.sax.saxutils import escape

from defusedxml import ElementTree
from icalendar import Calendar

from icloud_mcp.db.repositories import build_ics

DAV_NS = "DAV:"
NS = {"d": DAV_NS}


@dataclass(frozen=True)
class CalDAVConfig:
    """iCloud CalDAV discovery root."""

    root_url: str = "https://caldav.icloud.com/"


@dataclass(frozen=True)
class SyncedCalendar:
    """Discovered CalDAV calendar collection."""

    id: str
    url: str
    display_name: str
    color: str | None
    read_only: bool
    sync_token: str | None = None
    ctag: str | None = None


@dataclass(frozen=True)
class SyncedCalendarEvent:
    """CalDAV event normalized for local storage."""

    id: str
    calendar_id: str
    href: str
    uid: str
    etag: str | None
    raw_ics: str
    summary: str
    description: str | None
    location: str | None
    dtstart: str
    dtend: str
    timezone: str
    attendees: list[dict[str, str]]
    organizer: dict[str, str] | None
    rrule: str | None
    recurrence_id: str | None
    status: str | None


@dataclass(frozen=True)
class CalendarWrite:
    """Remote CalDAV write result."""

    href: str
    etag: str | None
    raw_ics: str
    uid: str


@dataclass(frozen=True)
class WebDAVSyncChange:
    """Changed WebDAV member returned by sync-collection."""

    href: str
    etag: str | None


@dataclass(frozen=True)
class WebDAVSyncResult:
    """Parsed WebDAV sync-collection result."""

    sync_token: str | None
    changed: list[WebDAVSyncChange]
    deleted: list[str]


class CalDAVCalendarAdapter:
    """Calendar read/write adapter using python-caldav."""

    def __init__(self, config: CalDAVConfig | None = None) -> None:
        self.config = config or CalDAVConfig()

    def configured(self, apple_id: str | None, app_password: str | None) -> bool:
        """Return whether credentials are available out-of-band."""

        return bool(apple_id and app_password)

    def discover(self, *, apple_id: str, app_password: str) -> list[SyncedCalendar]:
        """Discover iCloud calendars through CalDAV principal."""

        calendars = []
        with self._client(apple_id, app_password) as client:
            principal = client.principal()
            for calendar in principal.calendars():
                calendars.append(_calendar_from_object(calendar))
        return calendars

    def sync_events(
        self,
        *,
        apple_id: str,
        app_password: str,
        start: date,
        end: date,
    ) -> tuple[list[SyncedCalendar], list[SyncedCalendarEvent]]:
        """Fetch events in a time window from all writable/readable calendars."""

        calendars: list[SyncedCalendar] = []
        events: list[SyncedCalendarEvent] = []
        with self._client(apple_id, app_password) as client:
            principal = client.principal()
            for calendar_object in principal.calendars():
                calendar = _calendar_from_object(calendar_object)
                calendars.append(calendar)
                for event in calendar_object.search(start=start, end=end, event=True, expand=True):
                    data = _event_data(event)
                    if not data:
                        continue
                    events.append(_synced_event(calendar.id, event, data))
        return calendars, events

    def sync_event_changes(
        self,
        *,
        apple_id: str,
        app_password: str,
        calendar_id: str,
        calendar_url: str,
        sync_token: str,
    ) -> tuple[WebDAVSyncResult, list[SyncedCalendarEvent]]:
        """Fetch changed/deleted events using WebDAV sync-collection."""

        events: list[SyncedCalendarEvent] = []
        with self._client(apple_id, app_password) as client:
            calendar_object = _calendar_by_url(client, calendar_url)
            sync_client = getattr(calendar_object, "client", client)
            result = _sync_collection(sync_client, calendar_url, sync_token)
            for change in result.changed:
                event_href = urljoin(calendar_url, change.href)
                event = _event_by_url(client, event_href)
                data = _event_data(event)
                if data:
                    events.append(_synced_event(calendar_id, event, data))
        return result, events

    def create_event(
        self,
        *,
        apple_id: str,
        app_password: str,
        calendar_url: str,
        uid: str,
        title: str,
        start: str,
        end: str,
        timezone: str,
        location: str | None,
        description: str | None,
        attendees: list[dict[str, str]],
        recurrence: dict[str, Any] | None,
        alarms: list[dict[str, Any]],
    ) -> CalendarWrite:
        """Create event remotely and return href/ETag."""

        raw_ics = build_ics(
            uid=uid,
            title=title,
            start=start,
            end=end,
            timezone=timezone,
            location=location,
            description=description,
            attendees=attendees,
            recurrence=recurrence,
            alarms=alarms,
        )
        with self._client(apple_id, app_password) as client:
            calendar = _calendar_by_url(client, calendar_url)
            event = calendar.add_event(raw_ics)
            return CalendarWrite(href=str(getattr(event, "url", "")), etag=_etag(event), raw_ics=raw_ics, uid=uid)

    def update_event(
        self,
        *,
        apple_id: str,
        app_password: str,
        event_href: str,
        raw_ics: str,
        expected_etag: str | None,
    ) -> CalendarWrite | dict[str, Any]:
        """Replace remote event data, checking current ETag before save when available."""

        with self._client(apple_id, app_password) as client:
            event = _event_by_url(client, event_href)
            current_etag = _etag(event)
            if expected_etag and current_etag and expected_etag != current_etag:
                return {"status": "conflict", "latest_etag": current_etag}
            _save_event(event, raw_ics, expected_etag)
            return CalendarWrite(href=event_href, etag=_etag(event), raw_ics=raw_ics, uid=_uid_from_ics(raw_ics))

    def _client(self, apple_id: str, app_password: str) -> Any:
        from caldav import DAVClient

        return DAVClient(url=self.config.root_url, username=apple_id, password=app_password)


def _calendar_from_object(calendar: Any) -> SyncedCalendar:
    url = str(getattr(calendar, "url", ""))
    display_name = str(
        getattr(calendar, "name", None) or getattr(calendar, "calendar_name", None) or url.rsplit("/", 2)[-2]
    )
    return SyncedCalendar(
        id=f"cal_{hashlib.sha256(url.encode('utf-8')).hexdigest()[:16]}",
        url=url,
        display_name=display_name,
        color=_optional_attr(calendar, "color"),
        read_only=bool(getattr(calendar, "read_only", False)),
        sync_token=_call_optional(calendar, "get_sync_token") or _optional_attr(calendar, "sync_token"),
        ctag=_call_optional(calendar, "get_ctag") or _optional_attr(calendar, "ctag"),
    )


def _sync_collection(client: Any, url: str, sync_token: str | None) -> WebDAVSyncResult:
    response = client.report(url, _sync_collection_body(sync_token), depth=1)
    return _parse_sync_collection_response(_response_text(response))


def _sync_collection_body(sync_token: str | None) -> str:
    token = escape(sync_token or "")
    return f"""
    <d:sync-collection xmlns:d="DAV:">
      <d:sync-token>{token}</d:sync-token>
      <d:sync-level>1</d:sync-level>
      <d:prop>
        <d:getetag/>
      </d:prop>
    </d:sync-collection>
    """.strip()


def _parse_sync_collection_response(xml_text: str) -> WebDAVSyncResult:
    root = ElementTree.fromstring(xml_text)
    changed: list[WebDAVSyncChange] = []
    deleted: list[str] = []
    for response in root.findall("d:response", NS):
        href = _text(response, "d:href")
        if not href:
            continue
        status = _text(response, "d:status")
        if status and " 404 " in f" {status} ":
            deleted.append(href)
            continue
        changed.append(WebDAVSyncChange(href=href, etag=_text(response, ".//d:getetag")))
    return WebDAVSyncResult(sync_token=_text(root, "d:sync-token"), changed=changed, deleted=deleted)


def _response_text(response: Any) -> str:
    raw = getattr(response, "raw", None)
    if isinstance(raw, bytes):
        return raw.decode("utf-8", errors="replace")
    if isinstance(raw, str):
        return raw
    text = getattr(response, "text", None)
    if isinstance(text, str):
        return text
    return str(response)


def _synced_event(calendar_id: str, event: Any, raw_ics: str) -> SyncedCalendarEvent:
    parsed = _parse_ics(raw_ics)
    uid = parsed["uid"] or _uid_from_ics(raw_ics)
    href = str(getattr(event, "url", ""))
    return SyncedCalendarEvent(
        id=f"cal_evt_{hashlib.sha256((calendar_id + href).encode('utf-8')).hexdigest()[:24]}",
        calendar_id=calendar_id,
        href=href,
        uid=uid,
        etag=_etag(event),
        raw_ics=raw_ics,
        summary=parsed["summary"] or "(untitled)",
        description=parsed["description"],
        location=parsed["location"],
        dtstart=parsed["dtstart"],
        dtend=parsed["dtend"],
        timezone=parsed["timezone"],
        attendees=parsed["attendees"],
        organizer=parsed["organizer"],
        rrule=parsed["rrule"],
        recurrence_id=parsed["recurrence_id"],
        status=parsed["status"],
    )


def _parse_ics(raw_ics: str) -> dict[str, Any]:
    calendar = Calendar.from_ical(raw_ics)
    component = next((item for item in calendar.walk() if item.name == "VEVENT"), None)
    if component is None:
        raise ValueError("ICS does not contain VEVENT")
    dtstart = component.get("DTSTART")
    dtend = component.get("DTEND")
    start_value = _date_value(dtstart.dt if dtstart else None)
    end_value = _date_value(dtend.dt if dtend else dtstart.dt if dtstart else None)
    timezone = _timezone_name(dtstart.dt if dtstart else None)
    attendees = []
    for attendee in _as_list(component.get("ATTENDEE", [])):
        email = str(attendee).replace("mailto:", "")
        attendees.append({"email": email, "name": str(attendee.params.get("CN", email))})
    organizer = component.get("ORGANIZER")
    organizer_value = None
    if organizer:
        email = str(organizer).replace("mailto:", "")
        organizer_value = {"email": email, "name": str(organizer.params.get("CN", email))}
    rrule = component.get("RRULE")
    return {
        "uid": str(component.get("UID", "")),
        "summary": str(component.get("SUMMARY", "")),
        "description": str(component.get("DESCRIPTION", "")) or None,
        "location": str(component.get("LOCATION", "")) or None,
        "dtstart": start_value,
        "dtend": end_value,
        "timezone": timezone,
        "attendees": attendees,
        "organizer": organizer_value,
        "rrule": rrule.to_ical().decode("utf-8") if rrule else None,
        "recurrence_id": str(component.get("RECURRENCE-ID", "")) or None,
        "status": str(component.get("STATUS", "")) or None,
    }


def _date_value(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return datetime.now(tz=UTC).replace(microsecond=0).isoformat()


def _timezone_name(value: Any) -> str:
    tzinfo = getattr(value, "tzinfo", None)
    if tzinfo is None:
        return "UTC"
    return str(tzinfo)


def _event_data(event: Any) -> str | None:
    data = getattr(event, "data", None)
    if isinstance(data, bytes):
        return data.decode("utf-8", errors="replace")
    if isinstance(data, str):
        return data
    return None


def _save_event(event: Any, raw_ics: str, expected_etag: str | None) -> None:
    event.data = raw_ics
    if not expected_etag:
        event.save()
        return
    client = getattr(event, "client", None)
    url = getattr(event, "url", None)
    if client is None or not url or not hasattr(client, "put"):
        event.save()
        return
    response = client.put(
        url,
        raw_ics,
        {"Content-Type": 'text/calendar; charset="utf-8"', "If-Match": expected_etag},
    )
    _raise_for_write_failure(response)


def _raise_for_write_failure(response: Any) -> None:
    status = getattr(response, "status", None) or getattr(response, "status_code", None)
    if status in (200, 201, 204):
        return
    validate_status = getattr(response, "validate_status", None)
    if callable(validate_status):
        validate_status()
        return
    raise ValueError(f"CalDAV write failed with status {status}")


def _etag(event: Any) -> str | None:
    if hasattr(event, "get_etag"):
        try:
            return str(event.get_etag())
        except Exception:
            return None
    value = getattr(event, "etag", None)
    return str(value) if value else None


def _optional_attr(value: Any, name: str) -> str | None:
    attr = getattr(value, name, None)
    return str(attr) if attr else None


def _call_optional(value: Any, name: str) -> str | None:
    method = getattr(value, name, None)
    if not callable(method):
        return None
    try:
        result = method()
    except Exception:
        return None
    return str(result) if result else None


def _uid_from_ics(raw_ics: str) -> str:
    parsed = _parse_ics(raw_ics)
    return parsed["uid"] or f"uid-{hashlib.sha256(raw_ics.encode('utf-8')).hexdigest()[:16]}"


def _calendar_by_url(client: Any, calendar_url: str) -> Any:
    for calendar in client.principal().calendars():
        if str(getattr(calendar, "url", "")) == calendar_url:
            return calendar
    raise ValueError(f"Calendar URL not found: {calendar_url}")


def _event_by_url(client: Any, event_href: str) -> Any:
    if hasattr(client, "event_by_url"):
        return client.event_by_url(event_href)
    for calendar in client.principal().calendars():
        if hasattr(calendar, "event_by_url"):
            try:
                return calendar.event_by_url(event_href)
            except Exception:
                continue
    raise ValueError(f"Event URL not found: {event_href}")


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]


def _text(element: ElementTree.Element, path: str) -> str | None:
    found = element.find(path, NS)
    if found is None or found.text is None:
        return None
    return found.text.strip()
