"""Import operator-verified events (e.g. confirmed with organisers) from a JSON file.

The file is a JSON array of event objects. Events go through the same
normalisation, deduplication and change detection as fetched sources, under a
"manual" source that is never fetched. Events missing from a later import are
not deleted.
"""

import json
from datetime import date, datetime
from typing import Any

from django.db import IntegrityError, transaction

from apps.ingestion.models import SourceRun
from apps.ingestion.services import MANUAL_CONNECTOR, MAX_EVENTS_PER_RUN, run_ingestion
from apps.sources.models import Source
from connectors.base import AttendanceMode, EventStatus, RawEvent
from connectors.http import UnsafeURLError, check_url

MAX_FILE_BYTES = 2 * 1024 * 1024
REQUIRED_FIELDS = frozenset({"id", "title", "url", "starts_at"})
OPTIONAL_FIELDS = frozenset(
    {
        "ends_at",
        "timezone",
        "description",
        "venue_name",
        "address",
        "attendance_mode",
        "status",
        "organizer_name",
        "image_url",
    }
)
_ATTENDANCE = {mode.value for mode in AttendanceMode if mode.value}
_STATUSES = {status.value for status in EventStatus}


class ImportFileError(ValueError):
    pass


def _datetime(value: Any, field: str, index: int) -> datetime | date | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ImportFileError(f"Event {index}: {field} must be an ISO 8601 string")
    try:
        return date.fromisoformat(value) if len(value) == 10 else datetime.fromisoformat(value)
    except ValueError:
        raise ImportFileError(f"Event {index}: {field} is not a valid ISO 8601 date") from None


def _optional_text(row: dict[str, Any], field: str, index: int) -> str | None:
    value = row.get(field)
    if value is not None and not isinstance(value, str):
        raise ImportFileError(f"Event {index}: {field} must be a string")
    return value


def parse_event_file(content: bytes) -> list[RawEvent]:
    if len(content) > MAX_FILE_BYTES:
        raise ImportFileError(f"File is larger than {MAX_FILE_BYTES} bytes")
    try:
        rows = json.loads(content)
    except (ValueError, UnicodeDecodeError):
        raise ImportFileError("File is not valid JSON") from None
    if not isinstance(rows, list) or not rows:
        raise ImportFileError("File must be a non-empty JSON array of event objects")
    if len(rows) > MAX_EVENTS_PER_RUN:
        raise ImportFileError(f"File has more than {MAX_EVENTS_PER_RUN} events")

    events = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ImportFileError(f"Event {index}: must be a JSON object")
        unknown = sorted(set(row) - REQUIRED_FIELDS - OPTIONAL_FIELDS)
        if unknown:
            raise ImportFileError(f"Event {index}: unknown fields {unknown}")
        missing = sorted(field for field in REQUIRED_FIELDS if row.get(field) in (None, ""))
        if missing:
            raise ImportFileError(f"Event {index}: missing required fields {missing}")
        attendance = row.get("attendance_mode", "")
        if attendance and attendance not in _ATTENDANCE:
            raise ImportFileError(
                f"Event {index}: attendance_mode must be one of {sorted(_ATTENDANCE)}"
            )
        status = row.get("status", EventStatus.SCHEDULED.value)
        if status not in _STATUSES:
            raise ImportFileError(f"Event {index}: status must be one of {sorted(_STATUSES)}")
        for field in ("id", "title", "url"):
            if not isinstance(row[field], str):
                raise ImportFileError(f"Event {index}: {field} must be a string")

        events.append(
            RawEvent(
                external_id=row["id"],
                title=row["title"],
                url=row["url"],
                starts_at=_datetime(row["starts_at"], "starts_at", index),
                ends_at=_datetime(row.get("ends_at"), "ends_at", index),
                timezone=_optional_text(row, "timezone", index),
                description=_optional_text(row, "description", index),
                venue_name=_optional_text(row, "venue_name", index),
                address=_optional_text(row, "address", index),
                attendance_mode=AttendanceMode(attendance),
                status=EventStatus(status),
                organizer_name=_optional_text(row, "organizer_name", index),
                image_url=_optional_text(row, "image_url", index),
            )
        )
    return events


def get_or_create_manual_source(url: str, *, name: str, timezone: str) -> Source:
    try:
        parsed = check_url(url.strip())
    except UnsafeURLError as exc:
        raise ImportFileError(f"Source URL rejected: {exc.code}: {exc}") from None
    canonical = str(parsed).split("#", 1)[0]
    existing = Source.objects.filter(url=canonical).first()
    if existing is not None:
        if existing.connector != MANUAL_CONNECTOR:
            raise ImportFileError(
                f"Source #{existing.pk} already uses this URL with connector {existing.connector}"
            )
        return existing
    try:
        with transaction.atomic():
            return Source.objects.create(
                url=canonical,
                connector=MANUAL_CONNECTOR,
                name=name,
                timezone=timezone,
                status=Source.Status.APPROVED,
                review_notes="Created by import_events for operator-verified events",
            )
    except IntegrityError:
        return Source.objects.get(url=canonical, connector=MANUAL_CONNECTOR)


def import_events(content: bytes, *, source: Source) -> SourceRun:
    events = parse_event_file(content)
    return run_ingestion(source, trigger=SourceRun.Trigger.IMPORT, collect=lambda: events)
