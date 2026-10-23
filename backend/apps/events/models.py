from django.db import models

from pipeline.normalize import NormalizedEvent, to_snapshot


class Event(models.Model):
    """The current, normalised state of one event from one source."""

    class Status(models.TextChoices):
        SCHEDULED = "scheduled"
        CANCELLED = "cancelled"
        POSTPONED = "postponed"
        RESCHEDULED = "rescheduled"
        MOVED_ONLINE = "moved_online"

    class AttendanceMode(models.TextChoices):
        UNKNOWN = "", "Unknown"
        OFFLINE = "offline"
        ONLINE = "online"
        MIXED = "mixed"

    source = models.ForeignKey("sources.Source", on_delete=models.PROTECT, related_name="events")
    external_id = models.CharField(max_length=255)
    title = models.CharField(max_length=300)
    url = models.URLField(max_length=2000)
    starts_at = models.DateTimeField(null=True, blank=True, db_index=True)
    ends_at = models.DateTimeField(null=True, blank=True)
    is_all_day = models.BooleanField(default=False)
    timezone = models.CharField(max_length=64)
    description = models.TextField(blank=True)
    venue_name = models.CharField(max_length=300, blank=True)
    address = models.CharField(max_length=300, blank=True)
    attendance_mode = models.CharField(
        max_length=16, choices=AttendanceMode.choices, blank=True, default=""
    )
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.SCHEDULED)
    organizer_name = models.CharField(max_length=300, blank=True)
    image_url = models.URLField(max_length=2000, blank=True)
    attribution = models.CharField(
        max_length=500, blank=True, help_text="Credit or licence notice required by the source"
    )
    content_hash = models.CharField(max_length=64)
    first_seen_at = models.DateTimeField()
    last_seen_at = models.DateTimeField()
    last_changed_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["starts_at", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["source", "external_id"], name="event_unique_external_id_per_source"
            )
        ]

    def __str__(self) -> str:
        return self.title

    def to_normalized(self) -> NormalizedEvent:
        return NormalizedEvent(
            external_id=self.external_id,
            title=self.title,
            url=self.url,
            starts_at=self.starts_at,
            ends_at=self.ends_at,
            is_all_day=self.is_all_day,
            timezone=self.timezone,
            description=self.description,
            venue_name=self.venue_name,
            address=self.address,
            attendance_mode=self.attendance_mode,
            status=self.status,
            organizer_name=self.organizer_name,
            image_url=self.image_url,
            attribution=self.attribution,
        )

    def snapshot(self) -> dict:
        return to_snapshot(self.to_normalized())


class EventRevision(models.Model):
    """An append-only record of an event being created or changed by ingestion."""

    class Kind(models.TextChoices):
        CREATED = "created"
        UPDATED = "updated"

    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="revisions")
    source_run = models.ForeignKey(
        "ingestion.SourceRun",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="revisions",
    )
    kind = models.CharField(max_length=16, choices=Kind.choices)
    changed_fields = models.JSONField(default=list)
    before = models.JSONField(default=dict)
    after = models.JSONField(default=dict)
    is_significant = models.BooleanField(db_index=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self) -> str:
        return f"{self.kind} {self.event_id}: {', '.join(self.changed_fields)}"
