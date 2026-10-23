"""Private-chat commands. Process updates transactionally with an idempotent reply outbox."""

from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from apps.notifications.models import Delivery
from apps.sources.models import Source
from apps.sources.services import SourceRegistrationError, register_source
from apps.subscriptions.models import Subscriber, Subscription, TelegramCursor
from connectors.registry import all_connectors, match_connector

HELP = (
    "Event Bank finds tech events from supported sources.\n"
    "/sources — approved sources\n"
    "/platforms — supported platforms\n"
    "/add HTTPS_URL — submit a source for review\n"
    "/follow SOURCE_ID — consent to new-event and important-change alerts\n"
    "/unfollow SOURCE_ID — stop alerts for one source\n"
    "/stop — stop all alerts and remove follows\n"
    "Following sends future alerts, not a backlog. Only public event data is collected."
)


def _command(subscriber, text):
    command, _, argument = text.strip().partition(" ")
    command = command.split("@", 1)[0].lower()
    argument = argument.strip()
    if command in {"/start", "/help"}:
        return HELP
    if command == "/stop":
        subscriber.active = False
        subscriber.stopped_at = timezone.now()
        subscriber.save(update_fields=["active", "stopped_at"])
        subscriber.subscriptions.all().delete()
        Delivery.objects.filter(
            subscriber=subscriber, revision__isnull=False, status=Delivery.Status.PENDING
        ).update(
            status=Delivery.Status.CANCELLED, finished_at=timezone.now(), error_code="opted_out"
        )
        return "Alerts stopped and follows removed. Use /follow SOURCE_ID to opt in again."
    if command == "/platforms":
        names = "\n".join(f"- {cls.name}" for cls in all_connectors().values())
        return f"Supported sources:\n{names}\nOther platforms need a connector contribution."
    if command == "/sources":
        sources = Source.objects.filter(status=Source.Status.APPROVED).order_by("pk")[:30]
        names = "\n".join(f"{s.pk}: {s.name or s.connector}" for s in sources)
        return (names or "No approved sources yet.") + "\n/follow SOURCE_ID to receive alerts."
    if command in {"/follow", "/unfollow"}:
        if not argument.isascii() or not argument.isdecimal() or len(argument) > 10:
            return f"Usage: {command} SOURCE_ID"
        if command == "/unfollow":
            subscriber.subscriptions.filter(source_id=int(argument)).delete()
            Delivery.objects.filter(
                subscriber=subscriber,
                revision__event__source_id=int(argument),
                status=Delivery.Status.PENDING,
            ).update(
                status=Delivery.Status.CANCELLED,
                error_code="unfollowed",
                finished_at=timezone.now(),
            )
            return f"Unfollowed source #{argument}."
        source = Source.objects.filter(pk=int(argument), status=Source.Status.APPROVED).first()
        if source is None:
            return "Source not found or not approved. Use /sources."
        if subscriber.subscriptions.count() >= 50:
            return "You can follow up to 50 sources. Unfollow one first."
        Subscription.objects.get_or_create(subscriber=subscriber, source=source)
        subscriber.active = True
        subscriber.consented_at = timezone.now()
        subscriber.stopped_at = None
        subscriber.save(update_fields=["active", "consented_at", "stopped_at"])
        return (
            f"Following source #{source.pk}. You will receive future alerts. /stop to unsubscribe."
        )
    if command == "/add":
        if not argument or len(argument) > 2000 or any(char.isspace() for char in argument):
            return "Usage: /add HTTPS_URL"
        # Limit source requests per chat, including requests still awaiting review.
        since = timezone.now() - timedelta(days=1)
        if subscriber.source_requests.filter(created_at__gte=since).count() >= 5:
            return "You can submit five sources per day. Please try again tomorrow."
        try:
            # Do not accept user-controlled credentials or connector configuration over chat.
            source = register_source(argument)
        except SourceRegistrationError as exc:
            if exc.code == "duplicate_source":
                canonical = match_connector(argument).canonicalize_url(argument)
                existing = Source.objects.get(url=canonical)
                return f"Source #{existing.pk} already exists ({existing.status})."
            return f"Source not added: {exc.code}. Use /platforms to see supported sources."
        subscriber.source_requests.create(source=source)
        return (
            f"Source #{source.pk} submitted for operator review. "
            f"After approval, use /follow {source.pk} to opt in to alerts."
        )
    return HELP


def process_update(bot_id, update):
    if not isinstance(update, dict) or type(update.get("update_id")) is not int:
        return False
    update_id = update["update_id"]
    with transaction.atomic():
        cursor, _ = TelegramCursor.objects.get_or_create(bot_id=bot_id)
        cursor = TelegramCursor.objects.select_for_update().get(pk=cursor.pk)
        if update_id < cursor.next_update_id:
            return False
        message = update.get("message")
        message = message if isinstance(message, dict) else {}
        chat = message.get("chat")
        chat = chat if isinstance(chat, dict) else {}
        sender = message.get("from")
        sender = sender if isinstance(sender, dict) else {}
        text = message.get("text")
        chat_id = chat.get("id")
        if (
            chat.get("type") == "private"
            and type(chat_id) is int
            and 0 < chat_id < 10**20
            and sender.get("id") == chat_id
            and sender.get("is_bot") is False
            and isinstance(text, str)
            and len(text) <= 4096
        ):
            subscriber, _ = Subscriber.objects.get_or_create(telegram_chat_id=str(chat_id))
            subscriber = Subscriber.objects.select_for_update().get(pk=subscriber.pk)
            reply = _command(subscriber, text)
            Delivery.objects.get_or_create(
                dedupe_key=f"telegram:{bot_id}:update:{update_id}",
                defaults={"subscriber": subscriber, "message": reply},
            )
        cursor.next_update_id = update_id + 1
        cursor.save(update_fields=["next_update_id"])
    return True


def poll_commands(provider):
    cursor, _ = TelegramCursor.objects.get_or_create(bot_id=provider.bot_id)
    updates = provider.get_updates(cursor.next_update_id)
    valid = [u for u in updates if isinstance(u, dict) and type(u.get("update_id")) is int]
    for update in sorted(valid, key=lambda u: u["update_id"]):
        process_update(provider.bot_id, update)
    return len(valid)
