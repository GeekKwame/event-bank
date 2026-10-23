"""Transactional outbox: commit alerts with events, deliver outside transactions."""

from datetime import timedelta
from zoneinfo import ZoneInfo

from django.db import transaction
from django.utils import timezone

from apps.events.models import EventRevision
from apps.notifications.models import Delivery
from apps.notifications.providers.base import DeliveryOutcome
from apps.sources.models import Source
from apps.subscriptions.models import Subscription

MAX_ATTEMPTS = 3
CLAIM_TIMEOUT = timedelta(minutes=5)


def is_upcoming(event, now):
    end = event.ends_at or event.starts_at
    if event.starts_at is None or end is None:
        return False
    if event.is_all_day:
        zone = ZoneInfo(event.timezone)
        return end.astimezone(zone).date() >= now.astimezone(zone).date()
    return end >= now


def render_alert(revision):
    event = revision.event
    zone = ZoneInfo(event.timezone)
    when = event.starts_at.astimezone(zone)
    when = (
        when.strftime("%Y-%m-%d (all day)") if event.is_all_day else when.strftime("%Y-%m-%d %H:%M")
    )
    label = "New event" if revision.kind == EventRevision.Kind.CREATED else "Event updated"
    where = ", ".join(part for part in (event.venue_name, event.address) if part) or "Not provided"
    details = (
        f"{label}: {event.title[:160]}\n"
        f"When: {when} [{event.timezone}]\n"
        f"Where: {where[:200]}\n"
        f"Format: {event.attendance_mode or 'Not provided'} | Status: {event.status}\n"
    )
    # Dataset licenses/attribution must survive message delivery as well as storage.
    credit = event.description if event.source.connector in {"agentconfs", "confstech"} else ""
    suffix = f"\n{event.url}\nSource #{event.source_id}\n/stop to stop alerts."
    if credit:
        suffix += f"\n\n{credit}"
    budget = max(0, 4096 * 2 - len(suffix.encode("utf-16-le")))
    details = details.encode("utf-16-le")[:budget].decode("utf-16-le", errors="ignore")
    return details + suffix


def enqueue_run_alerts(run):
    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError("Alerts must be queued inside the ingestion transaction")
    now = timezone.now()
    subscriptions = list(
        Subscription.objects.filter(source_id=run.source_id, subscriber__active=True)
    )
    revisions = EventRevision.objects.filter(source_run=run, is_significant=True).select_related(
        "event__source"
    )
    for revision in revisions:
        if not is_upcoming(revision.event, now):
            continue
        message = render_alert(revision)
        for subscription in subscriptions:
            Delivery.objects.filter(
                subscriber_id=subscription.subscriber_id,
                revision__event_id=revision.event_id,
                status=Delivery.Status.PENDING,
            ).exclude(revision=revision).update(
                status=Delivery.Status.CANCELLED, error_code="superseded", finished_at=now
            )
            Delivery.objects.get_or_create(
                dedupe_key=f"revision:{revision.pk}:subscriber:{subscription.subscriber_id}",
                defaults={
                    "subscriber_id": subscription.subscriber_id,
                    "revision": revision,
                    "message": message,
                },
            )


def deliver_one(provider):
    """Return an attempted/cancelled delivery, or None. Caller handles pacing.

    Unknown sends are terminal: replaying after a timeout or worker crash could
    duplicate a message. Only explicit retryable results are scheduled again.
    """
    now = timezone.now()
    Delivery.objects.filter(
        status=Delivery.Status.SENDING, claimed_at__lt=now - CLAIM_TIMEOUT
    ).update(status=Delivery.Status.UNKNOWN, error_code="worker_interrupted", finished_at=now)
    with transaction.atomic():
        delivery = (
            Delivery.objects.select_for_update(skip_locked=True)
            .filter(status=Delivery.Status.PENDING, available_at__lte=now)
            .order_by("available_at", "pk")
            .first()
        )
        if delivery is None:
            return None
        subscriber = delivery.subscriber
        if delivery.revision_id:
            event = delivery.revision.event
            subscribed = Subscription.objects.filter(
                subscriber=subscriber, source_id=event.source_id
            ).exists()
            if (
                not subscriber.active
                or not subscribed
                or event.source.status != Source.Status.APPROVED
                or not is_upcoming(event, now)
            ):
                delivery.status = Delivery.Status.CANCELLED
                delivery.error_code = "no_longer_eligible"
                delivery.finished_at = now
                delivery.save()
                return delivery
        delivery.status = Delivery.Status.SENDING
        delivery.claimed_at = now
        delivery.attempts += 1
        delivery.save()

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
