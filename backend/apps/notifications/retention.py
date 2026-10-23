"""Delete personal data that is no longer needed (Telegram chat IDs and message bodies)."""

from datetime import timedelta

from django.conf import settings
from django.db.models import Q
from django.utils import timezone

from apps.notifications.models import Delivery
from apps.subscriptions.models import SourceRequest, Subscriber


def purge_expired_data(now=None) -> dict[str, int]:
    """Purge data older than DATA_RETENTION_DAYS. Pending or in-flight deliveries are kept.

    - finished deliveries (they hold message bodies and link a chat to events);
    - /add attempt records (only needed for daily rate limits);
    - subscribers with no follows who stopped, or never opted in, before the cutoff and have
      had no activity since.
    """
    now = now or timezone.now()
    cutoff = now - timedelta(days=settings.DATA_RETENTION_DAYS)
    deliveries, _ = (
        Delivery.objects.filter(finished_at__lt=cutoff)
        .exclude(status__in=[Delivery.Status.PENDING, Delivery.Status.SENDING])
        .delete()
    )
    requests, _ = SourceRequest.objects.filter(created_at__lt=cutoff).delete()
    dormant = (
        Subscriber.objects.filter(active=False, subscriptions__isnull=True)
        .filter(Q(stopped_at__lt=cutoff) | Q(stopped_at__isnull=True, created_at__lt=cutoff))
        .exclude(delivery__created_at__gte=cutoff)
        .exclude(source_requests__created_at__gte=cutoff)
        .values("pk")
    )
    subscribers, _ = Subscriber.objects.filter(pk__in=dormant).delete()
    return {"deliveries": deliveries, "source_requests": requests, "subscribers": subscribers}
