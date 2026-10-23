from django.conf import settings
from django.core.management.base import BaseCommand

from apps.notifications.retention import purge_expired_data


class Command(BaseCommand):
    help = "Delete finished deliveries, /add attempts and dormant subscribers past retention."

    def handle(self, *args, **options):
        counts = purge_expired_data()
        self.stdout.write(
            f"Retention {settings.DATA_RETENTION_DAYS} days: deleted "
            + ", ".join(f"{count} {name}" for name, count in counts.items())
        )
