"""Extract schema.org Event data from JSON-LD blocks in HTML pages."""

import json
import re
from collections.abc import Iterator, Mapping
from datetime import date, datetime
from typing import Any
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from connectors.base import AttendanceMode, EventStatus, RawEvent

EVENT_TYPES = frozenset(
    {
        "Event",
        "BusinessEvent",
        "ChildrensEvent",
        "ComedyEvent",
        "CourseInstance",
        "DanceEvent",
        "DeliveryEvent",
        "EducationEvent",
        "EventSeries",
        "ExhibitionEvent",
        "Festival",
        "FoodEvent",
        "Hackathon",
        "LiteraryEvent",
        "MusicEvent",
        "PublicationEvent",
        "SaleEvent",
        "ScreeningEvent",
        "SocialEvent",
        "SportsEvent",
        "TheaterEvent",
        "VisualArtsEvent",
    }
)

_STATUS = {
    "EventScheduled": EventStatus.SCHEDULED,
    "EventCancelled": EventStatus.CANCELLED,
    "EventPostponed": EventStatus.POSTPONED,
    "EventRescheduled": EventStatus.RESCHEDULED,
    "EventMovedOnline": EventStatus.MOVED_ONLINE,
}
_ATTENDANCE = {
    "OfflineEventAttendanceMode": AttendanceMode.OFFLINE,
    "OnlineEventAttendanceMode": AttendanceMode.ONLINE,
    "MixedEventAttendanceMode": AttendanceMode.MIXED,
}
_JSONLD_TYPE = re.compile(r"^\s*application/ld\+json\s*(;.*)?$", re.IGNORECASE)
_DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_SCHEMA_PREFIX = re.compile(r"^(?:https?://schema\.org/|schema:)", re.IGNORECASE)


def _strip_schema_prefix(value: str) -> str:
    return _SCHEMA_PREFIX.sub("", value.strip())


def _flatten(node: Any) -> Iterator[Mapping[str, Any]]:
    if isinstance(node, list):
        for item in node:
            yield from _flatten(item)
    elif isinstance(node, Mapping):
        yield node
        if "@graph" in node:
            yield from _flatten(node["@graph"])


def iter_jsonld_objects(html: str) -> Iterator[Mapping[str, Any]]:
    """Yield every JSON object in the page's JSON-LD blocks, skipping invalid JSON."""
    soup = BeautifulSoup(html, "html.parser")
    for script in soup.find_all("script", attrs={"type": _JSONLD_TYPE}):
        try:
            data = json.loads(script.get_text())
        except ValueError:
            continue
        yield from _flatten(data)


def types_of(node: Mapping[str, Any]) -> set[str]:
    raw = node.get("@type")
    values = raw if isinstance(raw, list) else [raw]
    return {_strip_schema_prefix(value) for value in values if isinstance(value, str)}


def is_event(node: Mapping[str, Any]) -> bool:
    return bool(types_of(node) & EVENT_TYPES)


def find_events(html: str) -> list[Mapping[str, Any]]:
    return [node for node in iter_jsonld_objects(html) if is_event(node)]


def text_value(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        return text_value(value.get("@value") or value.get("name"))
    if isinstance(value, list) and value:
        return text_value(value[0])
    return None


def parse_datetime(value: Any) -> datetime | date | None:
    text = text_value(value)
    if not text:
        return None
    text = text.strip()
    try:
        if _DATE_ONLY.match(text):
            return date.fromisoformat(text)
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _address_text(address: Any) -> str | None:
    if isinstance(address, str):
        return address
    if not isinstance(address, Mapping):
        return None
    country = address.get("addressCountry")
    parts = [
        address.get("streetAddress"),
        address.get("addressLocality"),
        address.get("addressRegion"),
        address.get("postalCode"),
        text_value(country) if country is not None else None,
    ]
    return ", ".join(part.strip() for part in parts if isinstance(part, str) and part.strip())


def _locations(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _image_url(value: Any) -> str | None:
    if isinstance(value, list):
        return _image_url(value[0]) if value else None
    if isinstance(value, Mapping):
        return text_value(value.get("url") or value.get("contentUrl"))
    return text_value(value)


def to_raw_event(node: Mapping[str, Any], *, external_id: str, base_url: str) -> RawEvent:
    venue_name = address = None
    has_physical = has_virtual = False
    for location in _locations(node.get("location")):
        if isinstance(location, str):
            has_physical = True
            venue_name = venue_name or location
            continue
        if not isinstance(location, Mapping):
            continue
        if "VirtualLocation" in types_of(location):
            has_virtual = True
            continue
        has_physical = True
        venue_name = venue_name or text_value(location.get("name"))
        address = address or _address_text(location.get("address"))

    mode_text = text_value(node.get("eventAttendanceMode"))
    attendance = _ATTENDANCE.get(_strip_schema_prefix(mode_text)) if mode_text else None
    if attendance is None:
        if has_physical and has_virtual:
            attendance = AttendanceMode.MIXED
        elif has_virtual:
            attendance = AttendanceMode.ONLINE
        elif has_physical:
            attendance = AttendanceMode.OFFLINE
        else:
            attendance = AttendanceMode.UNKNOWN

    status_text = text_value(node.get("eventStatus"))
    status = _STATUS.get(_strip_schema_prefix(status_text)) if status_text else None

    url = text_value(node.get("url"))
    return RawEvent(
        external_id=external_id,
        title=text_value(node.get("name")) or "",
        url=urljoin(base_url, url) if url else base_url,
        starts_at=parse_datetime(node.get("startDate")),
        ends_at=parse_datetime(node.get("endDate")),
        description=text_value(node.get("description")),
        venue_name=venue_name,
        address=address,
        attendance_mode=attendance,
        status=status or EventStatus.SCHEDULED,
        organizer_name=text_value(node.get("organizer")),
        image_url=_image_url(node.get("image")),
    )
