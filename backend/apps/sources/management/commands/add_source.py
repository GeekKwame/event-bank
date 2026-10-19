from django.core.management.base import BaseCommand, CommandError

from apps.sources.services import SourceRegistrationError, register_source


def parse_config(pairs: list[str]) -> dict[str, str]:
    config = {}
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if not sep or not key.strip():
            raise CommandError(f"invalid_config: expected KEY=VALUE, got {pair!r}")
        config[key.strip()] = value.strip()
    return config


class Command(BaseCommand):
    help = "Register an event source URL. Sources start pending unless --approve is given."

    def add_arguments(self, parser):
        parser.add_argument("url")
        parser.add_argument("--name", default="")
        parser.add_argument("--timezone", default=None, help="IANA timezone, e.g. Africa/Accra")
        parser.add_argument(
            "--config",
            action="append",
            default=[],
            metavar="KEY=VALUE",
            help="Connector-specific setting; repeatable. Secrets are never passed this way.",
        )
        parser.add_argument(
            "--approve",
            action="store_true",
            help="Approve the source immediately (you are acting as the reviewing administrator)",
        )

    def handle(self, *args, url, name, timezone, config, approve, **options):
        try:
            source = register_source(
                url, name=name, timezone=timezone, config=parse_config(config), approve=approve
            )
        except SourceRegistrationError as exc:
            raise CommandError(f"{exc.code}: {exc}") from None
        self.stdout.write(
            self.style.SUCCESS(
                f"Registered source #{source.pk} ({source.url}) "
                f"connector={source.connector} status={source.status}"
            )
        )
