"""Public Luma calendars and city pages through Luma's free iCal subscription feeds.

Luma documents "Add iCal Subscription" for calendars and Discover city pages
(https://help.luma.com/p/ical-syncing): the feeds need no API key or Luma Plus.
Register the exact link that button gives, with `webcal://` changed to `https://`.
Personal feeds (`entity=user`) carry a private token and are rejected.

Luma documents these feeds for calendar apps. Whether storing their events and
notifying subscribers is permitted is awaiting Luma's written confirmation, so
live feeds must not be ingested until then.

The feed hosts below are the ones Luma's subscription links have been observed to
use; Luma does not document them.
"""

import re
from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, ClassVar
from urllib.parse import parse_qsl, urlsplit

import icalendar

from connectors.base import (
    AttendanceMode,
    Connector,
    ConnectorError,
    EventStatus,
    Fetcher,
    RawEvent,
    SourceSpec,
)
from connectors.registry import register

FEED_HOSTS = frozenset({"api.lu.ma", "api2.luma.com"})
FEED_PATH = "/ics/get"
LOOKBACK = timedelta(days=1)

_ENTITY_IDS = {
    "calendar": re.compile(r"cal-[A-Za-z0-9]{1,64}"),
    "discover": re.compile(r"discplace-[A-Za-z0-9]{1,64}"),
}
_LUMA_PAGE_URL = re.compile(r"https://(?:www\.)?(?:luma\.com|lu\.ma)/[A-Za-z0-9][A-Za-z0-9_/-]*")
_ANY_URL = re.compile(r"^\s*https?://", re.IGNORECASE)
_HIDDEN_CLASSES = frozenset({"PRIVATE", "CONFIDENTIAL"})
_PARSE_ERRORS = (ValueError, IndexError, KeyError, TypeError)


def feed_entity(url: str) -> tuple[str, str] | None:
    """Return (entity, id) for a public calendar or city feed URL, else None."""
    parts = urlsplit(url)
    if parts.path != FEED_PATH or parts.fragment:
        return None
    params = parse_qsl(parts.query, keep_blank_values=True)
    if sorted(key for key, _ in params) != ["entity", "id"]:
        return None
    values = dict(params)
    pattern = _ENTITY_IDS.get(values["entity"])
    if pattern is None or not pattern.fullmatch(values["id"]):
        return None
    return values["entity"], values["id"]


def _text(component: Any, name: str) -> str | None:
    value = component.get(name)
    return str(value).strip() or None if value is not None else None


def _as_utc(value: datetime | date) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    return datetime.combine(value, time.min, tzinfo=UTC)


@register
class LumaICalConnector(Connector):
    slug = "luma-ical"
    name = "Luma calendar or city (public iCal feed)"
    allowed_hosts = FEED_HOSTS
    accepted_content_types = frozenset({"text/calendar"})

    clock: ClassVar[Callable[[], datetime]] = staticmethod(lambda: datetime.now(UTC))

    @classmethod
    def matches(cls, url: str) -> bool:
        return super().matches(url) and feed_entity(url) is not None

    @classmethod
    def canonicalize_url(cls, url: str) -> str:
        entity, entity_id = feed_entity(url) or ("", "")
        host = (urlsplit(url).hostname or "").lower()
        return f"https://{host}{FEED_PATH}?entity={entity}&id={entity_id}"

    def collect(self, source: SourceSpec, fetcher: Fetcher) -> Iterator[RawEvent]:
        response = fetcher.get(source.url)
        try:
            calendar = icalendar.Calendar.from_ical(response.text)
        except _PARSE_ERRORS:
            raise ConnectorError(
                "Feed is not valid iCalendar", code="unexpected_response"
            ) from None
        if getattr(calendar, "name", None) != "VCALENDAR":
            raise ConnectorError("Feed is not an iCalendar VCALENDAR", code="unexpected_response")

        cutoff = self.clock() - LOOKBACK
        for component in calendar.walk("VEVENT"):
            try:
                raw = self.to_raw_event(component)
            except _PARSE_ERRORS:
                continue
            if raw is None:
                continue
            end = raw.ends_at or raw.starts_at
            if end is not None and _as_utc(end) < cutoff:
                continue
            yield raw

    @staticmethod
    def to_raw_event(component: Any) -> RawEvent | None:
        """Map one VEVENT, or return None for events that must not be published.

        ORGANIZER and ATTENDEE are never read: they can hold people's names and emails.
        A LOCATION that is a link (e.g. a meeting URL) is not stored.
        """
        if (_text(component, "CLASS") or "PUBLIC").upper() in _HIDDEN_CLASSES:
            return None
        uid = _text(component, "UID")
        if uid is None or component.get("DTSTART") is None:
            return None
        recurrence = component.get("RECURRENCE-ID")
        external_id = f"{uid}#{recurrence.dt.isoformat()}" if recurrence is not None else uid

        starts_at = component.get("DTSTART").dt
        if component.get("DTEND") is not None:
            ends_at = component.get("DTEND").dt
        elif component.get("DURATION") is not None:
            ends_at = starts_at + component.get("DURATION").dt
        else:
            ends_at = None
        tz_name = getattr(getattr(starts_at, "tzinfo", None), "key", None)

        description = _text(component, "DESCRIPTION")
        location = _text(component, "LOCATION")
        if location and _ANY_URL.match(location):
            address, attendance = None, AttendanceMode.ONLINE
        elif location:
            address, attendance = location, AttendanceMode.OFFLINE
        else:
            address, attendance = None, AttendanceMode.UNKNOWN

        url = _text(component, "URL")
        if not url:
            match = _LUMA_PAGE_URL.search(description or "") or _LUMA_PAGE_URL.search(
                location or ""
            )
            url = match.group(0) if match else ""

        status = (_text(component, "STATUS") or "").upper()
        return RawEvent(
            external_id=external_id,
            title=_text(component, "SUMMARY") or "",
            url=url,
            starts_at=starts_at,
            ends_at=ends_at,
            timezone=tz_name,
            description=description,
            address=address,
            attendance_mode=attendance,
            status=EventStatus.CANCELLED if status == "CANCELLED" else EventStatus.SCHEDULED,
        )
