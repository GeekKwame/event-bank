from django.db import models


class SourceRun(models.Model):
    """One attempt to ingest a source. Created before fetching so failures are always recorded."""

    class Status(models.TextChoices):
        RUNNING = "running"
        SUCCEEDED = "succeeded"
        FAILED = "failed"

    class Trigger(models.TextChoices):
        MANUAL = "manual"
        SCHEDULED = "scheduled"
        IMPORT = "import"

    source = models.ForeignKey("sources.Source", on_delete=models.CASCADE, related_name="runs")
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.RUNNING, db_index=True
    )
    trigger = models.CharField(max_length=16, choices=Trigger.choices, default=Trigger.MANUAL)
    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    events_found = models.PositiveIntegerField(default=0)
    events_created = models.PositiveIntegerField(default=0)
    events_updated = models.PositiveIntegerField(default=0)
    events_unchanged = models.PositiveIntegerField(default=0)
    events_skipped = models.PositiveIntegerField(default=0)
    skipped_details = models.JSONField(default=list, blank=True)
    error_code = models.CharField(max_length=64, blank=True)
    error_message = models.CharField(max_length=1000, blank=True)

    class Meta:
        ordering = ["-started_at", "-id"]

    def __str__(self) -> str:
        return f"Run {self.pk} of source {self.source_id}: {self.status}"
