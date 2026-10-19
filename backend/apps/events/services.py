from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import datetime

from django.db import transaction

from apps.events.models import Event, EventRevision
from pipeline.diff import diff_snapshots
from pipeline.fingerprint import fingerprint
from pipeline.normalize import TRACKED_FIELDS, NormalizedEvent, to_snapshot


@dataclass
class UpsertResult:
    created: int = 0
    updated: int = 0
    unchanged: int = 0


def _field_values(event: NormalizedEvent) -> dict:
    values = asdict(event)
    values.pop("external_id")
    return values


def upsert_events(
    source, events: Sequence[NormalizedEvent], *, now: datetime, source_run=None
) -> UpsertResult:
    """Create or update events for one source and record a revision for every change.

    Events absent from `events` are left untouched: one missing fetch is not
    evidence that an event was removed. Must run inside a transaction.
    """
    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError("upsert_events must be called inside transaction.atomic()")

    result = UpsertResult()
    existing = {
        event.external_id: event
        for event in Event.objects.select_for_update().filter(
            source=source, external_id__in=[event.external_id for event in events]
        )
    }
    for incoming in events:
        snapshot = to_snapshot(incoming)
        content_hash = fingerprint(snapshot)
        current = existing.get(incoming.external_id)

        if current is None:
            event = Event.objects.create(
                source=source,
                external_id=incoming.external_id,
                content_hash=content_hash,
                first_seen_at=now,
                last_seen_at=now,
                last_changed_at=now,
                **_field_values(incoming),
            )
            EventRevision.objects.create(
                event=event,
                source_run=source_run,
                kind=EventRevision.Kind.CREATED,
                changed_fields=list(TRACKED_FIELDS),
                before={},
                after=snapshot,
                is_significant=True,
            )
            result.created += 1
            continue

        current.last_seen_at = now
        change = diff_snapshots(current.snapshot(), snapshot)
        if not change.has_changes:
            current.content_hash = content_hash
            current.save(update_fields=["last_seen_at", "content_hash", "updated_at"])
            result.unchanged += 1
            continue

        for name, value in _field_values(incoming).items():
            setattr(current, name, value)
        current.content_hash = content_hash
        current.last_changed_at = now
        current.save()
        EventRevision.objects.create(
            event=current,
            source_run=source_run,
            kind=EventRevision.Kind.UPDATED,
            changed_fields=list(change.changed_fields),
            before=change.before,
            after=change.after,
            is_significant=change.is_significant,
        )
        result.updated += 1
    return result
