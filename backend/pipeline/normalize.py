"""Turn connector output into a canonical form. Pure Python: no Django, no I/O."""

import html
import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass, fields
from datetime import UTC, date, datetime, time
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from connectors.base import AttendanceMode, EventStatus, RawEvent

MAX_EXTERNAL_ID_LENGTH = 255
MAX_TITLE_LENGTH = 300
MAX_TEXT_LENGTH = 300
MAX_DESCRIPTION_LENGTH = 5000
MAX_URL_LENGTH = 2000
MIN_YEAR, MAX_YEAR = 2000, 2100

_TRACKING_PARAM = re.compile(r"^(utm_[a-z]+|fbclid|gclid|mc_cid|mc_eid)$", re.IGNORECASE)
_WHITESPACE = re.compile(r"\s+")
_BLANK_LINES = re.compile(r"\n{3,}")


class NormalizationError(ValueError):
    pass


@dataclass(frozen=True)
class NormalizedEvent:
    external_id: str
    title: str
    url: str
    starts_at: datetime | None
    ends_at: datetime | None
    is_all_day: bool
    timezone: str
    description: str
    venue_name: str
    address: str
    attendance_mode: str
    status: str
    organizer_name: str
    image_url: str


TRACKED_FIELDS = tuple(f.name for f in fields(NormalizedEvent) if f.name != "external_id")


@dataclass(frozen=True)
class SkippedEvent:
    external_id: str
    reason: str


@dataclass(frozen=True)
class NormalizationResult:
    events: tuple[NormalizedEvent, ...]
    skipped: tuple[SkippedEvent, ...]


def to_snapshot(event: NormalizedEvent) -> dict[str, Any]:
    """JSON-serialisable view of the tracked fields, used for fingerprints and diffs."""
    snapshot: dict[str, Any] = {}
    for name in TRACKED_FIELDS:
        value = getattr(event, name)
        snapshot[name] = value.astimezone(UTC).isoformat() if isinstance(value, datetime) else value
    return snapshot


def _strip_controls(value: str) -> str:
    return "".join(char for char in value if char in "\n\t" or unicodedata.category(char)[0] != "C")


def clean_line(value: str | None, max_length: int = MAX_TEXT_LENGTH) -> str:
    if not value:
        return ""
    text = _WHITESPACE.sub(" ", _strip_controls(html.unescape(value))).strip()
    return text[:max_length]


def clean_paragraphs(value: str | None, max_length: int = MAX_DESCRIPTION_LENGTH) -> str:
    if not value:
        return ""
    text = _strip_controls(html.unescape(value).replace("\r\n", "\n").replace("\r", "\n"))
    lines = [_WHITESPACE.sub(" ", line).strip() for line in text.split("\n")]
    return _BLANK_LINES.sub("\n\n", "\n".join(lines)).strip()[:max_length]


def canonicalize_url(value: str | None) -> str:
    """Lowercase scheme and host, drop fragments, default ports and tracking parameters.

    Returns "" for anything that is not an absolute http(s) URL, so links such as
    `javascript:` can never reach a notification.
    """
    if not value:
        return ""
    try:
        parts = urlsplit(value.strip())
        port = parts.port
    except ValueError:
        return ""
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        return ""
    scheme = parts.scheme.lower()
    host = parts.hostname.lower()
    if port and (scheme, port) not in (("http", 80), ("https", 443)):
        host = f"{host}:{port}"
    query = urlencode(
        sorted(
            (key, val)
            for key, val in parse_qsl(parts.query, keep_blank_values=True)
            if not _TRACKING_PARAM.match(key)
        )
    )
    url = urlunsplit((scheme, host, parts.path or "/", query, ""))
    return url if len(url) <= MAX_URL_LENGTH else ""


def resolve_timezone(name: str | None, default: str) -> str:
    for candidate in (name, default):
        if not candidate:
            continue
        try:
            ZoneInfo(candidate)
        except (ZoneInfoNotFoundError, ValueError):
            continue
        return candidate
    raise NormalizationError(f"Unknown timezone {default!r}")


def to_utc(value: datetime | date | None, tz_name: str) -> tuple[datetime | None, bool]:
    """Return (UTC datetime, is_all_day). Naive values are interpreted in `tz_name`."""
    if value is None:
        return None, False
    tz = ZoneInfo(tz_name)
    if isinstance(value, datetime):
        localized = value if value.tzinfo is not None else value.replace(tzinfo=tz)
        result, all_day = localized.astimezone(UTC), False
    else:
        result, all_day = datetime.combine(value, time.min, tzinfo=tz).astimezone(UTC), True
    if not MIN_YEAR <= result.year <= MAX_YEAR:
        return None, False
    return result, all_day


def normalize_event(raw: RawEvent, *, default_timezone: str) -> NormalizedEvent:
    external_id = clean_line(raw.external_id, MAX_EXTERNAL_ID_LENGTH + 1)
    if not external_id:
        raise NormalizationError("missing_external_id")
    if len(external_id) > MAX_EXTERNAL_ID_LENGTH:
        raise NormalizationError("external_id_too_long")
    title = clean_line(raw.title, MAX_TITLE_LENGTH)
    if not title:
        raise NormalizationError("missing_title")
    url = canonicalize_url(raw.url)
    if not url:
        raise NormalizationError("invalid_url")

    tz_name = resolve_timezone(raw.timezone, default_timezone)
    starts_at, is_all_day = to_utc(raw.starts_at, tz_name)
    ends_at, _ = to_utc(raw.ends_at, tz_name)
    if starts_at and ends_at and ends_at < starts_at:
        ends_at = None
    try:
        attendance_mode = AttendanceMode(raw.attendance_mode).value
        status = EventStatus(raw.status).value
    except ValueError:
        raise NormalizationError("invalid_status_or_attendance_mode") from None

    return NormalizedEvent(
        external_id=external_id,
        title=title,
        url=url,
        starts_at=starts_at,
        ends_at=ends_at,
        is_all_day=is_all_day,
        timezone=tz_name,
        description=clean_paragraphs(raw.description),
        venue_name=clean_line(raw.venue_name),
        address=clean_line(raw.address),
        attendance_mode=attendance_mode,
        status=status,
        organizer_name=clean_line(raw.organizer_name),
        image_url=canonicalize_url(raw.image_url),
    )


def normalize_events(
    raw_events: Iterable[RawEvent], *, default_timezone: str
) -> NormalizationResult:
    events: list[NormalizedEvent] = []
    skipped: list[SkippedEvent] = []
    seen: set[str] = set()
    for raw in raw_events:
        try:
            event = normalize_event(raw, default_timezone=default_timezone)
        except NormalizationError as exc:
            skipped.append(SkippedEvent(external_id=str(raw.external_id)[:100], reason=str(exc)))
            continue
        if event.external_id in seen:
            skipped.append(SkippedEvent(external_id=event.external_id, reason="duplicate"))
            continue
        seen.add(event.external_id)
        events.append(event)
    return NormalizationResult(events=tuple(events), skipped=tuple(skipped))
