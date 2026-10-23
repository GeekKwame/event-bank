"""Small deployments use one PostgreSQL-backed worker; no Redis or Celery required."""

from datetime import timedelta

from django.conf import settings
from django.db.models import Max
from django.utils import timezone

from apps.ingestion.models import SourceRun
from apps.ingestion.services import IngestionError, ingest_source
from apps.sources.models import Source


def ingest_due_sources(*, ingest=ingest_source):
    if not settings.INGESTION_SCHEDULING_ENABLED:
        return 0
    cutoff = timezone.now() - timedelta(seconds=settings.INGESTION_INTERVAL_SECONDS)
    sources = (
        Source.objects.filter(status=Source.Status.APPROVED)
        .annotate(last_attempt=Max("runs__started_at"))
        .order_by("last_attempt", "pk")
    )
    count = 0
    for source in sources:
        if source.last_attempt is not None and source.last_attempt > cutoff:
            continue
        try:
            ingest(source.pk, trigger=SourceRun.Trigger.SCHEDULED)
        except IngestionError:
            # An operator may pause a source or start a manual ingestion meanwhile.
            continue
        count += 1
        if count >= 20:
            break
    return count
