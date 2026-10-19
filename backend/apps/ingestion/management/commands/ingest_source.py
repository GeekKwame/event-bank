from django.core.management.base import BaseCommand, CommandError

from apps.ingestion.models import SourceRun
from apps.ingestion.services import IngestionError, ingest_source
from apps.sources.models import Source


class Command(BaseCommand):
    help = "Fetch one approved source and store its events."

    def add_arguments(self, parser):
        parser.add_argument("source_id", type=int)

    def handle(self, *args, source_id, **options):
        try:
            run = ingest_source(source_id)
        except Source.DoesNotExist:
            raise CommandError(f"source_not_found: no source with id {source_id}") from None
        except IngestionError as exc:
            raise CommandError(f"{exc.code}: {exc}") from None

        if run.status != SourceRun.Status.SUCCEEDED:
            raise CommandError(f"Run #{run.pk} failed: {run.error_code}: {run.error_message}")
        self.stdout.write(
            self.style.SUCCESS(
                f"Run #{run.pk} succeeded: found={run.events_found} created={run.events_created} "
                f"updated={run.events_updated} unchanged={run.events_unchanged} "
                f"skipped={run.events_skipped}"
            )
        )
