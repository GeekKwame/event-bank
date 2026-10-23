from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.ingestion.imports import ImportFileError, get_or_create_manual_source, import_events
from apps.ingestion.models import SourceRun


class Command(BaseCommand):
    help = (
        "Import operator-verified events from a JSON file into a manual source "
        "(created and approved on first use)."
    )

    def add_arguments(self, parser):
        parser.add_argument("file", type=Path)
        parser.add_argument(
            "--source-url",
            required=True,
            help="https URL identifying who supplied the events, e.g. the organiser's website",
        )
        parser.add_argument("--source-name", default="")
        parser.add_argument("--timezone", default=None, help="Default IANA timezone for the file")

    def handle(self, *args, file, source_url, source_name, timezone, **options):
        tz_name = timezone or settings.TIME_ZONE
        try:
            ZoneInfo(tz_name)
        except (ZoneInfoNotFoundError, ValueError):
            raise CommandError(f"invalid_timezone: Unknown timezone {tz_name!r}") from None
        try:
            content = file.read_bytes()
        except OSError as exc:
            raise CommandError(f"Cannot read {file}: {exc.strerror}") from None

        try:
            source = get_or_create_manual_source(source_url, name=source_name, timezone=tz_name)
            run = import_events(content, source=source)
        except ImportFileError as exc:
            raise CommandError(f"invalid_file: {exc}") from None

        if run.status != SourceRun.Status.SUCCEEDED:
            raise CommandError(f"Run #{run.pk} failed: {run.error_code}: {run.error_message}")
        for skipped in run.skipped_details:
            self.stderr.write(f"Skipped {skipped['external_id']}: {skipped['reason']}")
        self.stdout.write(
            self.style.SUCCESS(
                f"Run #{run.pk} for source #{source.pk} succeeded: found={run.events_found} "
                f"created={run.events_created} updated={run.events_updated} "
                f"unchanged={run.events_unchanged} skipped={run.events_skipped}"
            )
        )
