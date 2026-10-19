import logging
import re
from collections.abc import Iterable

REDACTED = "[REDACTED]"
MIN_SECRET_LENGTH = 4

_SECRET_QUERY_PARAM = re.compile(
    r"(?i)\b(key|api_key|apikey|token|access_token|secret|password|signature)=([^&\s'\"]+)"
)


def redact(text: str, secrets: Iterable[str] = ()) -> str:
    for secret in secrets:
        if secret and len(secret) >= MIN_SECRET_LENGTH:
            text = text.replace(secret, REDACTED)
    return _SECRET_QUERY_PARAM.sub(lambda match: f"{match.group(1)}={REDACTED}", text)


def configured_secrets() -> list[str]:
    from django.conf import settings

    return [
        settings.SECRET_KEY,
        settings.MNOTIFY_API_KEY,
        settings.DATABASES["default"].get("PASSWORD", ""),
    ]


class RedactingFormatter(logging.Formatter):
    """Redacts configured secrets and secret-looking query parameters, including in tracebacks."""

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record), configured_secrets())
