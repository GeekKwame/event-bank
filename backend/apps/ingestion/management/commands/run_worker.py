import time

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection

from apps.ingestion.worker import ingest_due_sources
from apps.notifications.providers.base import ProviderConfigurationError
from apps.notifications.providers.telegram import TelegramProvider
from apps.notifications.services import deliver_one
from apps.subscriptions.telegram import poll_commands

WORKER_LOCK_ID = 1837460201


class Command(BaseCommand):
    help = "Poll Telegram commands, ingest due sources, and deliver queued alerts."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="Process one cycle then exit")

    def handle(self, *args, once, **options):
        provider = None
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_try_advisory_lock(%s)", [WORKER_LOCK_ID])
            acquired = cursor.fetchone()[0]
        if not acquired:
            raise CommandError("An Event Bank worker is already running for this database")
        try:
            if settings.TELEGRAM_SENDING_ENABLED:
                provider = TelegramProvider()
            while True:
                telegram_ready = provider is not None
                if provider:
                    try:
                        poll_commands(provider)
                    except ProviderConfigurationError as exc:
                        # This exception contains only our fixed, credential-free message.
                        self.stderr.write(str(exc))
                        telegram_ready = False
                ingested = ingest_due_sources()
                delivered = 0
                if telegram_ready:
                    for _ in range(20):
                        if deliver_one(provider) is None:
                            break
                        delivered += 1
                        # Private chats only; keep below Telegram's free per-chat limits.
                        time.sleep(1.1)
                self.stdout.write(
                    f"Ingested {ingested} source(s); processed {delivered} delivery(s)"
                )
                if once:
                    break
                time.sleep(5)
        except ProviderConfigurationError as exc:
            raise CommandError(str(exc)) from None
        except KeyboardInterrupt:
            self.stdout.write("Worker stopped")
        finally:
            if provider:
                provider.close()
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_unlock(%s)", [WORKER_LOCK_ID])
