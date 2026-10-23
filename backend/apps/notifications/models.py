from django.db import models
from django.utils import timezone


class Delivery(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending"
        SENDING = "sending"
        ACCEPTED = "accepted"
        REJECTED = "rejected"
        UNKNOWN = "unknown"
        CANCELLED = "cancelled"

    class Kind(models.TextChoices):
        REPLY = "reply", "Command reply"
        ALERT = "alert", "Event alert"
        DIGEST = "digest", "Alert digest"

    subscriber = models.ForeignKey("subscriptions.Subscriber", on_delete=models.CASCADE)
    kind = models.CharField(max_length=8, choices=Kind.choices, default=Kind.ALERT)
    revision = models.ForeignKey(
        "events.EventRevision", null=True, blank=True, on_delete=models.CASCADE
    )
    source = models.ForeignKey(
        "sources.Source",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        help_text="Set for digests, which summarise several revisions from one source.",
    )
    dedupe_key = models.CharField(max_length=120, unique=True)
    message = models.TextField()
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    available_at = models.DateTimeField(default=timezone.now)
    claimed_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    attempts = models.PositiveIntegerField(default=0)
    provider_message_id = models.CharField(max_length=200, blank=True)
    error_code = models.CharField(max_length=64, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=["status", "available_at"], name="delivery_due_idx")]

    def __str__(self):
        return f"Delivery #{self.pk}: {self.status}"
