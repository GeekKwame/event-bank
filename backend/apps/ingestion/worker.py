"""Small deployments use one PostgreSQL-backed worker; no Redis or Celery required."""

import logging
from datetime import timedelta

from django.conf import settings
from django.db.models import Max
from django.utils import timezone

from apps.ingestion.models import SourceRun
from apps.ingestion.services import IngestionError, ingest_source
from apps.sources.models import Source

logger = logging.getLogger(__name__)


def ingest_due_sources(*, ingest=ingest_source, limit=None):
    """Ingest up to `limit` approved sources whose last attempt is older than the interval.

    The limit keeps one worker cycle short, so Telegram polling and delivery are not
    starved. A failing source is logged and skipped; it never stops the cycle.
    """
    if not settings.INGESTION_SCHEDULING_ENABLED:
        return 0
    limit = settings.INGESTION_SOURCES_PER_CYCLE if limit is None else limit
    cutoff = timezone.now() - timedelta(seconds=settings.INGESTION_INTERVAL_SECONDS)
    sources = (
        Source.objects.filter(status=Source.Status.APPROVED)
        .annotate(last_attempt=Max("runs__started_at"))
        .order_by("last_attempt", "pk")
        .values_list("pk", "last_attempt")
    )
    count = 0
    for source_id, last_attempt in sources:
        if count >= limit:
            break
        if last_attempt is not None and last_attempt > cutoff:
            continue
        try:
            ingest(source_id, trigger=SourceRun.Trigger.SCHEDULED)
        except (IngestionError, Source.DoesNotExist):
            # Paused, deleted, busy or manual sources are skipped until the next cycle.
            continue
        except Exception:
            logger.exception("Scheduled ingestion of source %s failed unexpectedly", source_id)
            continue
        count += 1
    return count
