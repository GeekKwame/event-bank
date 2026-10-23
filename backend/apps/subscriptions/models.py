from django.db import models


class Subscriber(models.Model):
    """A private Telegram chat that can explicitly opt in to source alerts."""

    telegram_chat_id = models.CharField(max_length=32, unique=True)
    active = models.BooleanField(default=False)
    consented_at = models.DateTimeField(null=True, blank=True)
    stopped_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Subscriber #{self.pk}"


class Subscription(models.Model):
    subscriber = models.ForeignKey(
        Subscriber, on_delete=models.CASCADE, related_name="subscriptions"
    )
    source = models.ForeignKey(
        "sources.Source", on_delete=models.CASCADE, related_name="subscriptions"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["subscriber", "source"], name="unique_source_subscription"
            )
        ]

    def __str__(self):
        return f"Subscriber #{self.subscriber_id} follows source #{self.source_id}"


class TelegramCursor(models.Model):
    """Per-bot polling offset; credentials and incoming message bodies are not stored."""

    bot_id = models.CharField(max_length=32, unique=True)
    next_update_id = models.BigIntegerField(default=0)

    def __str__(self):
        return f"Telegram bot #{self.bot_id}"


class SourceRequest(models.Model):
    """One /add attempt. Failed attempts are recorded too, so they count towards rate limits."""

    subscriber = models.ForeignKey(
        Subscriber, on_delete=models.CASCADE, related_name="source_requests"
    )
    source = models.ForeignKey("sources.Source", null=True, blank=True, on_delete=models.CASCADE)
    outcome = models.CharField(max_length=32, default="submitted")
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Source request #{self.pk}"
