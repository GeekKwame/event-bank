"""Transactional outbox: commit alerts with events, deliver outside transactions."""

from datetime import timedelta
from zoneinfo import ZoneInfo

from django.db import transaction
from django.db.models import Case, IntegerField, Q, Value, When
from django.utils import timezone

from apps.events.models import EventRevision
from apps.notifications.models import Delivery
from apps.notifications.providers.base import DeliveryOutcome
from apps.sources.models import Source
from apps.subscriptions.models import Subscription

MAX_ATTEMPTS = 3
CLAIM_TIMEOUT = timedelta(minutes=5)
# Above this many alerts from one ingestion run, a subscriber gets one digest instead.
MAX_ALERTS_PER_RUN = 5
DIGEST_EVENTS_LISTED = 5
TELEGRAM_MAX_CHARS = 4096


def is_upcoming(event, now):
    end = event.ends_at or event.starts_at
    if event.starts_at is None or end is None:
        return False
    if event.is_all_day:
        zone = ZoneInfo(event.timezone)
        return end.astimezone(zone).date() >= now.astimezone(zone).date()
    return end >= now


def _fit(details: str, suffix: str) -> str:
    """Trim `details` so the message fits Telegram's limit (counted in UTF-16 code units)."""
    budget = max(0, TELEGRAM_MAX_CHARS * 2 - len(suffix.encode("utf-16-le")))
    details = details.encode("utf-16-le")[:budget].decode("utf-16-le", errors="ignore")
    return details + suffix


def _when(event) -> str:
    when = event.starts_at.astimezone(ZoneInfo(event.timezone))
    return (
        when.strftime("%Y-%m-%d (all day)") if event.is_all_day else when.strftime("%Y-%m-%d %H:%M")
    )


def render_alert(revision):
    event = revision.event
    label = "New event" if revision.kind == EventRevision.Kind.CREATED else "Event updated"
    where = ", ".join(part for part in (event.venue_name, event.address) if part) or "Not provided"
    details = (
        f"{label}: {event.title[:160]}\n"
        f"When: {_when(event)} [{event.timezone}]\n"
        f"Where: {where[:200]}\n"
        f"Format: {event.attendance_mode or 'Not provided'} | Status: {event.status}\n"
    )
    suffix = f"\n{event.url}\nSource #{event.source_id}\n/stop to stop alerts."
    if event.attribution:
        suffix += f"\n{event.attribution}"
    return _fit(details, suffix)


def render_digest(source, revisions):
    shown = revisions[:DIGEST_EVENTS_LISTED]
    lines = [
        f"{len(revisions)} new or updated upcoming events from {source} (source #{source.pk}):",
        "",
    ]
    for revision in shown:
        event = revision.event
        lines.append(f"- {event.title[:120]} ({_when(event)})\n  {event.url}")
    if len(revisions) > len(shown):
        lines.append(f"…and {len(revisions) - len(shown)} more.")
    attributions = sorted({r.event.attribution for r in revisions if r.event.attribution})
    suffix = "\n/unfollow " + str(source.pk) + " or /stop to stop alerts."
    if attributions:
        suffix += "\n" + "\n".join(attributions)
    return _fit("\n".join(lines), suffix)


def enqueue_run_alerts(run):
    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError("Alerts must be queued inside the ingestion transaction")
    now = timezone.now()
    subscriber_ids = list(
        Subscription.objects.filter(source_id=run.source_id, subscriber__active=True).values_list(
            "subscriber_id", flat=True
        )
    )
    if not subscriber_ids:
        return
    revisions = [
        revision
        for revision in EventRevision.objects.filter(source_run=run, is_significant=True)
        .select_related("event__source")
        .order_by("event__starts_at", "pk")
        if is_upcoming(revision.event, now)
    ]
    if not revisions:
        return

    for subscriber_id in subscriber_ids:
        # A newer significant revision replaces any older pending alert for the same event.
        Delivery.objects.filter(
            subscriber_id=subscriber_id,
            kind=Delivery.Kind.ALERT,
            revision__event_id__in=[r.event_id for r in revisions],
            status=Delivery.Status.PENDING,
        ).exclude(revision__in=revisions).update(
            status=Delivery.Status.CANCELLED, error_code="superseded", finished_at=now
        )
        if len(revisions) > MAX_ALERTS_PER_RUN:
            Delivery.objects.get_or_create(
                dedupe_key=f"run:{run.pk}:subscriber:{subscriber_id}:digest",
                defaults={
                    "subscriber_id": subscriber_id,
                    "kind": Delivery.Kind.DIGEST,
                    "source_id": run.source_id,
                    "message": render_digest(run.source, revisions),
                },
            )
            continue
        for revision in revisions:
            Delivery.objects.get_or_create(
                dedupe_key=f"revision:{revision.pk}:subscriber:{subscriber_id}",
                defaults={
                    "subscriber_id": subscriber_id,
                    "kind": Delivery.Kind.ALERT,
                    "revision": revision,
                    "message": render_alert(revision),
                },
            )


def cancel_pending_alerts(subscriber, *, source_id=None, reason, now=None):
    """Cancel queued alerts and digests (not command replies) for a subscriber."""
    pending = Delivery.objects.filter(
        subscriber=subscriber,
        kind__in=[Delivery.Kind.ALERT, Delivery.Kind.DIGEST],
        status=Delivery.Status.PENDING,
    )
    if source_id is not None:
        pending = pending.filter(Q(revision__event__source_id=source_id) | Q(source_id=source_id))
    return pending.update(
        status=Delivery.Status.CANCELLED, error_code=reason, finished_at=now or timezone.now()
    )


def _still_eligible(delivery, now) -> bool:
    """Re-check consent and relevance just before sending. Replies are always sent."""
    subscriber = delivery.subscriber
    if delivery.revision_id is not None:
        event = delivery.revision.event
        source = event.source
        upcoming = is_upcoming(event, now)
    elif delivery.source_id is not None:
        source = delivery.source
        upcoming = True
    else:
        return True
    return (
        subscriber.active
        and source.status == Source.Status.APPROVED
        and Subscription.objects.filter(subscriber=subscriber, source=source).exists()
        and upcoming
    )


def deliver_one(provider):
    """Return an attempted/cancelled delivery, or None. Caller handles pacing.

    Command replies go first, so a /stop confirmation never waits behind alerts.
    Unknown sends are terminal: replaying after a timeout or worker crash could
    duplicate a message. Only explicit retryable results are scheduled again.
    """
    now = timezone.now()
    Delivery.objects.filter(
        status=Delivery.Status.SENDING, claimed_at__lt=now - CLAIM_TIMEOUT
    ).update(status=Delivery.Status.UNKNOWN, error_code="worker_interrupted", finished_at=now)
    with transaction.atomic():
        delivery = (
            Delivery.objects.select_for_update(skip_locked=True, of=("self",))
            .filter(status=Delivery.Status.PENDING, available_at__lte=now)
            .annotate(
                priority=Case(
                    When(kind=Delivery.Kind.REPLY, then=Value(0)),
                    default=Value(1),
                    output_field=IntegerField(),
                )
            )
            .select_related("subscriber", "source", "revision__event__source")
            .order_by("priority", "available_at", "pk")
            .first()
        )
        if delivery is None:
            return None
        if not _still_eligible(delivery, now):
            delivery.status = Delivery.Status.CANCELLED
            delivery.error_code = "no_longer_eligible"
            delivery.finished_at = now
            delivery.save()
            return delivery
        delivery.status = Delivery.Status.SENDING
        delivery.claimed_at = now
        delivery.attempts += 1
        delivery.save()

    subscriber = delivery.subscriber
    try:
        result = provider.send([subscriber.telegram_chat_id], delivery.message)
    except Exception:
        # Do not expose third-party exceptions, which may contain credentials.
        delivery.status = Delivery.Status.UNKNOWN
        delivery.error_code = "provider_exception"
    else:
        delivery.error_code = (result.error_code or "")[:64]
        delivery.provider_message_id = (result.provider_message_id or "")[:200]
        if result.outcome == DeliveryOutcome.RETRYABLE:
            if delivery.attempts < MAX_ATTEMPTS:
                delivery.status = Delivery.Status.PENDING
                delay = max(result.retry_after or 0, 60 * 2 ** (delivery.attempts - 1))
                delivery.available_at = timezone.now() + timedelta(seconds=delay)
            else:
                delivery.status = Delivery.Status.REJECTED
                delivery.error_code = "retry_exhausted"
        else:
            delivery.status = {
                DeliveryOutcome.ACCEPTED: Delivery.Status.ACCEPTED,
                DeliveryOutcome.REJECTED: Delivery.Status.REJECTED,
                DeliveryOutcome.UNKNOWN: Delivery.Status.UNKNOWN,
            }[result.outcome]
    delivery.finished_at = None if delivery.status == Delivery.Status.PENDING else timezone.now()
    # A recovered UNKNOWN row must not be resurrected by a late worker response.
    Delivery.objects.filter(pk=delivery.pk, status=Delivery.Status.SENDING).update(
        status=delivery.status,
        available_at=delivery.available_at,
        finished_at=delivery.finished_at,
        error_code=delivery.error_code,
        provider_message_id=delivery.provider_message_id,
    )
    return delivery
