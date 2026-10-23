import logging
import time

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import close_old_connections, connection

from apps.ingestion.worker import ingest_due_sources
from apps.notifications.providers.base import ProviderConfigurationError
from apps.notifications.providers.telegram import TelegramProvider
from apps.notifications.retention import purge_expired_data
from apps.notifications.services import deliver_one
from apps.subscriptions.telegram import poll_commands

logger = logging.getLogger(__name__)

WORKER_LOCK_ID = 1837460201
IDLE_SLEEP_SECONDS = 5
MAX_BACKOFF_SECONDS = 300
DELIVERIES_PER_CYCLE = 20
SEND_INTERVAL_SECONDS = 1.1
PURGE_INTERVAL_SECONDS = 3600


def _try_lock() -> bool:
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_lock(%s)", [WORKER_LOCK_ID])
        return cursor.fetchone()[0]


def _holds_lock() -> bool:
    """Advisory locks belong to a database session, which a silent reconnect loses."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE locktype = 'advisory' AND granted "
            "AND pid = pg_backend_pid() AND classid = 0 AND objid = %s AND objsubid = 1)",
            [WORKER_LOCK_ID],
        )
        return cursor.fetchone()[0]


class Command(BaseCommand):
    help = "Poll Telegram commands, ingest due sources, and deliver queued alerts."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="Process one cycle then exit")

    def handle(self, *args, once, **options):
        if not _try_lock():
            raise CommandError("An Event Bank worker is already running for this database")
        provider = None
        try:
            if settings.TELEGRAM_SENDING_ENABLED:
                provider = TelegramProvider()
            failures = 0
            last_purge = None
            while True:
                try:
                    if not _holds_lock() and not _try_lock():
                        raise CommandError("Lost the worker lock to another worker; stopping")
                    if (
                        last_purge is None
                        or time.monotonic() - last_purge >= PURGE_INTERVAL_SECONDS
                    ):
                        purge_expired_data()
                        last_purge = time.monotonic()
                    failures = 0 if self._cycle(provider) else failures + 1
                except CommandError:
                    raise
                except Exception:
                    failures += 1
                    logger.exception("Worker cycle failed; retrying after a pause")
                    close_old_connections()
                if once:
                    break
                time.sleep(min(MAX_BACKOFF_SECONDS, IDLE_SLEEP_SECONDS * 2 ** min(failures, 6)))
        except ProviderConfigurationError as exc:
            # Contains only our fixed, credential-free message.
            raise CommandError(str(exc)) from None
        except KeyboardInterrupt:
            self.stdout.write("Worker stopped")
        finally:
            if provider:
                provider.close()
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT pg_advisory_unlock(%s)", [WORKER_LOCK_ID])
            except Exception:
                logger.warning("Could not release the worker lock; it ends with the session")

    def _cycle(self, provider) -> bool:
        """Run one cycle. Returns False if Telegram was unreachable (so the worker backs off)."""
        telegram_ok = provider is not None
        if telegram_ok:
            try:
                poll_commands(provider)
            except ProviderConfigurationError as exc:
                # Contains only our fixed, credential-free message.
                self.stderr.write(str(exc))
                telegram_ok = False
        ingested = ingest_due_sources()
        delivered = 0
        if telegram_ok:
            for _ in range(DELIVERIES_PER_CYCLE):
                if deliver_one(provider) is None:
                    break
                delivered += 1
                # Private chats only; keep below Telegram's free per-chat limits.
                time.sleep(SEND_INTERVAL_SECONDS)
        if ingested or delivered:
            self.stdout.write(f"Ingested {ingested} source(s); processed {delivered} delivery(s)")
        return telegram_ok or provider is None
