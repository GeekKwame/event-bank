import itertools
import logging
import os
from collections.abc import Callable, Mapping
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from apps.common.redaction import configured_secrets, redact
from apps.events.services import upsert_events
from apps.ingestion.models import SourceRun
from apps.notifications.services import enqueue_run_alerts
from apps.sources.models import Source
from apps.sources.services import fetch_policy_for
from connectors.base import ConnectorError, Fetcher, SourceSpec
from connectors.http import FetchError, FetchPolicy, SafeFetcher
from connectors.registry import UnsupportedSourceError, get_connector
from pipeline.normalize import normalize_events

logger = logging.getLogger(__name__)

MAX_EVENTS_PER_RUN = 500
MAX_SKIPPED_DETAILS = 50
RUN_LEASE = timedelta(minutes=15)

FetcherFactory = Callable[[FetchPolicy], Fetcher]


class IngestionError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class SourceNotApprovedError(IngestionError):
    pass


def _error_details(exc: Exception) -> tuple[str, str]:
    if isinstance(exc, FetchError | ConnectorError | IngestionError):
        code, message = exc.code, str(exc)
    elif isinstance(exc, UnsupportedSourceError):
        code, message = "unsupported_source", str(exc)
    else:
        # Unexpected errors may carry arbitrary data, so only the type is stored.
        code, message = "internal_error", type(exc).__name__
    return code, redact(message, configured_secrets())[:1000]


def ingest_source(
    source_id: int,
    *,
    trigger: str = SourceRun.Trigger.MANUAL,
    fetcher_factory: FetcherFactory = SafeFetcher,
    environ: Mapping[str, str] = os.environ,
) -> SourceRun:
    """Fetch one approved source and upsert its events in a single transaction.

    Network I/O happens before the transaction opens. If anything fails, the
    run is marked failed and no event rows are changed.
    """
    with transaction.atomic():
        source = Source.objects.select_for_update().get(pk=source_id)
        if not source.is_approved:
            raise SourceNotApprovedError(
                "source_not_approved", f"Source #{source.pk} is {source.status}, not approved"
            )
        now = timezone.now()
        running = SourceRun.objects.filter(source=source, status=SourceRun.Status.RUNNING)
        if running.filter(started_at__gte=now - RUN_LEASE).exists():
            raise IngestionError("source_busy", "This source already has an ingestion in progress")
        running.update(status=SourceRun.Status.FAILED, finished_at=now, error_code="lease_expired")
        run = SourceRun.objects.create(source=source, trigger=trigger)
    try:
        connector_cls = get_connector(source.connector)
        config = connector_cls.validate_config(source.config)
        spec = SourceSpec(
            url=source.url,
            timezone=source.timezone,
            config=config,
            credentials=connector_cls.credentials_from_env(config, environ),
        )
        fetcher = fetcher_factory(fetch_policy_for(connector_cls))
        try:
            raw_events = list(
                itertools.islice(connector_cls().collect(spec, fetcher), MAX_EVENTS_PER_RUN + 1)
            )
        finally:
            close = getattr(fetcher, "close", None)
            if close is not None:
                close()
        if len(raw_events) > MAX_EVENTS_PER_RUN:
            raise IngestionError(
                "too_many_events", f"Source returned more than {MAX_EVENTS_PER_RUN} events"
            )

        result = normalize_events(raw_events, default_timezone=source.timezone)
        if not result.events:
            raise IngestionError(
                "no_events", "No valid events found; the page format may have changed"
            )

        with transaction.atomic():
            current_source = Source.objects.select_for_update().get(pk=source.pk)
            if not current_source.is_approved:
                raise SourceNotApprovedError(
                    "source_not_approved", "Source was paused during fetch"
                )
            if not SourceRun.objects.filter(pk=run.pk, status=SourceRun.Status.RUNNING).exists():
                raise IngestionError("run_superseded", "A newer run replaced this expired run")
            upserted = upsert_events(source, result.events, now=timezone.now(), source_run=run)
            enqueue_run_alerts(run)
            run.status = SourceRun.Status.SUCCEEDED
            run.finished_at = timezone.now()
            run.events_found = len(raw_events)
            run.events_created = upserted.created
            run.events_updated = upserted.updated
            run.events_unchanged = upserted.unchanged
            run.events_skipped = len(result.skipped)
            run.skipped_details = [
                {"external_id": skipped.external_id, "reason": skipped.reason}
                for skipped in result.skipped[:MAX_SKIPPED_DETAILS]
            ]
            run.save()
    except Exception as exc:
        code, message = _error_details(exc)
        if code == "internal_error":
            logger.exception("Unexpected error ingesting source %s", source.pk)
        else:
            logger.warning("Ingesting source %s failed: %s: %s", source.pk, code, message)
        run.status = SourceRun.Status.FAILED
        run.finished_at = timezone.now()
        run.events_created = run.events_updated = run.events_unchanged = 0
        run.error_code = code
        run.error_message = message
        run.save()
    return run
