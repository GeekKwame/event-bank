from django.contrib import admin

from apps.events.admin import ReadOnlyAdmin
from apps.ingestion.models import SourceRun


@admin.register(SourceRun)
class SourceRunAdmin(ReadOnlyAdmin):
    list_display = [
        "id",
        "source",
        "status",
        "started_at",
        "events_created",
        "events_updated",
        "events_unchanged",
        "events_skipped",
        "error_code",
    ]
    list_filter = ["status", "trigger", "error_code"]
