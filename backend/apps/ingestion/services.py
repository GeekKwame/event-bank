import itertools
import logging
import os
from collections.abc import Callable, Mapping, Sequence
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from apps.common.redaction import configured_secrets, redact
from apps.events.services import upsert_events
from apps.ingestion.models import SourceRun
from apps.notifications.services import enqueue_run_alerts
from apps.sources.models import Source
from apps.sources.services import fetch_policy_for
from connectors.base import ConnectorError, Fetcher, RawEvent, SourceSpec
from connectors.http import FetchError, FetchPolicy, SafeFetcher
from connectors.registry import UnsupportedSourceError, get_connector
from pipeline.normalize import normalize_events

logger = logging.getLogger(__name__)

MAX_EVENTS_PER_RUN = 500
MAX_SKIPPED_DETAILS = 50
RUN_LEASE = timedelta(minutes=15)
MANUAL_CONNECTOR = "manual"

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


def start_run(source_id: int, *, trigger: str) -> tuple[Source, SourceRun]:
    """Create a running SourceRun, unless the source is unapproved or already being ingested.

    A run older than RUN_LEASE is presumed dead and expired, so a crashed worker can't
    block a source forever; the expired run can no longer commit (see run_ingestion).
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
        return source, SourceRun.objects.create(source=source, trigger=trigger)


def run_ingestion(
    source: Source, run: SourceRun, collect: Callable[[], Sequence[RawEvent]]
) -> SourceRun:
    """Collect raw events, then normalise, upsert and queue alerts in one transaction.

    `collect` does any I/O before the transaction opens. If anything fails, the run
    is marked failed and no event rows or alerts are written.
    """
    try:
        raw_events = list(collect())
        truncated = len(raw_events) > MAX_EVENTS_PER_RUN
        if truncated:
            # Missing events are never deleted, so processing a prefix is safe.
            raw_events = raw_events[:MAX_EVENTS_PER_RUN]

        result = normalize_events(raw_events, default_timezone=source.timezone)
        if not result.events:
            raise IngestionError(
                "no_events", "No valid events found; the page format may have changed"
            )
        skipped_details = [
            {"external_id": skipped.external_id, "reason": skipped.reason}
            for skipped in result.skipped[:MAX_SKIPPED_DETAILS]
        ]
        if truncated:
            skipped_details.insert(
                0,
                {
                    "external_id": "",
                    "reason": f"run_limit_reached: only the first {MAX_EVENTS_PER_RUN} "
                    "events were processed",
                },
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
            run.skipped_details = skipped_details
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
        # Only finish our own run: an expired run may already have been closed by start_run.
        SourceRun.objects.filter(pk=run.pk, status=SourceRun.Status.RUNNING).update(
            status=run.status,
            finished_at=run.finished_at,
            events_created=0,
            events_updated=0,
            events_unchanged=0,
            error_code=code,
            error_message=message,
        )
    return run


def ingest_source(
    source_id: int,
    *,
    trigger: str = SourceRun.Trigger.MANUAL,
    fetcher_factory: FetcherFactory = SafeFetcher,
    environ: Mapping[str, str] = os.environ,
) -> SourceRun:
    """Fetch one approved source through its connector and store its events."""
    source = Source.objects.get(pk=source_id)
    if source.connector == MANUAL_CONNECTOR:
        raise IngestionError(
            "manual_source", f"Source #{source.pk} is updated with import_events, not fetched"
        )
    source, run = start_run(source_id, trigger=trigger)

    def collect() -> list[RawEvent]:
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
            return list(
                itertools.islice(connector_cls().collect(spec, fetcher), MAX_EVENTS_PER_RUN + 1)
            )
        finally:
            close = getattr(fetcher, "close", None)
            if close is not None:
                close()

    return run_ingestion(source, run, collect)
