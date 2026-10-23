import logging
import os
import re
from collections.abc import Iterable

REDACTED = "[REDACTED]"
# Shorter values (e.g. "true" in FEATURE_TOKEN_ENABLED) would redact ordinary words.
MIN_SECRET_LENGTH = 8

_SECRET_QUERY_PARAM = re.compile(
    r"(?i)\b(key|api_key|apikey|token|access_token|secret|password|signature)=([^&\s'\"]+)"
)


def redact(text: str, secrets: Iterable[str] = ()) -> str:
    for secret in secrets:
        if secret and len(secret) >= MIN_SECRET_LENGTH:
            text = text.replace(secret, REDACTED)
    return _SECRET_QUERY_PARAM.sub(lambda match: f"{match.group(1)}={REDACTED}", text)


_SECRET_ENV_NAME = re.compile(r"(KEY|SECRET|TOKEN|PASSWORD)", re.IGNORECASE)


def configured_secrets() -> list[str]:
    """Secrets from settings plus any environment variable whose name looks secret."""
    from django.conf import settings

    return [
        settings.SECRET_KEY,
        settings.MNOTIFY_API_KEY,
        settings.TELEGRAM_BOT_TOKEN,
        settings.DATABASES["default"].get("PASSWORD", ""),
        *(value for name, value in os.environ.items() if _SECRET_ENV_NAME.search(name)),
    ]


class RedactingFormatter(logging.Formatter):
    """Redacts configured secrets and secret-looking query parameters, including in tracebacks."""

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record), configured_secrets())
