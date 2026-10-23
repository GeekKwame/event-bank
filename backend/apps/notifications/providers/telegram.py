"""Official Telegram Bot API. Paid broadcasts are explicitly disabled."""

import re
from collections.abc import Sequence

import httpx
from django.conf import settings

from apps.notifications.providers.base import (
    DeliveryOutcome,
    NotificationProvider,
    ProviderConfigurationError,
    SendingDisabledError,
    SendResult,
)

_TOKEN = re.compile(r"[0-9]{1,20}:[A-Za-z0-9_-]{20,}")
_CHAT = re.compile(r"[1-9][0-9]{0,19}")
_NOT_SENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)


class TelegramProvider(NotificationProvider):
    name = "telegram"
    channel = "telegram"

    def __init__(self, token: str | None = None, client: httpx.Client | None = None):
        self.token = token if token is not None else settings.TELEGRAM_BOT_TOKEN
        if not _TOKEN.fullmatch(self.token):
            raise ProviderConfigurationError("TELEGRAM_BOT_TOKEN is missing or invalid")
        if client is None and not settings.TELEGRAM_SENDING_ENABLED:
            raise SendingDisabledError("Telegram sending is disabled")
        self.bot_id = self.token.split(":", 1)[0]
        self.client = client or httpx.Client(timeout=15, trust_env=False, follow_redirects=False)

    def close(self):
        self.client.close()

    def send(self, recipients: Sequence[str], message: str) -> SendResult:
        def result(outcome, **kwargs):
            return SendResult(provider=self.name, outcome=outcome, **kwargs)

        if (
            len(recipients) != 1
            or not _CHAT.fullmatch(recipients[0])
            or not message.strip()
            or len(message.encode("utf-16-le")) // 2 > 4096
        ):
            return result(DeliveryOutcome.REJECTED, error_code="invalid_request")
        try:
            response = self.client.post(
                f"https://api.telegram.org/bot{self.token}/sendMessage",
                json={
                    "chat_id": recipients[0],
                    "text": message,
                    "allow_paid_broadcast": False,
                    "link_preview_options": {"is_disabled": True},
                },
            )
        except _NOT_SENT:
            return result(DeliveryOutcome.RETRYABLE, error_code="connect_failed")
        except httpx.HTTPError:
            # Never store the exception or URL: the token is in the path.
            return result(DeliveryOutcome.UNKNOWN, error_code="transport_error")
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        code = payload.get("error_code", response.status_code)
        if response.status_code == 429 or code == 429:
            params = payload.get("parameters")
            retry = params.get("retry_after") if isinstance(params, dict) else None
            retry = retry if type(retry) is int and 0 < retry <= 604800 else 60
            return result(DeliveryOutcome.RETRYABLE, error_code="rate_limited", retry_after=retry)
        if response.status_code >= 500 or (type(code) is int and code >= 500):
            return result(DeliveryOutcome.UNKNOWN, error_code="server_error")
        if response.status_code in (400, 401, 403, 404) or code in (400, 401, 403, 404):
            return result(DeliveryOutcome.REJECTED, error_code=f"telegram_{code}")
        data = payload.get("result")
        if (
            response.status_code == 200
            and payload.get("ok") is True
            and isinstance(data, dict)
            and type(data.get("message_id")) is int
        ):
            return result(
                DeliveryOutcome.ACCEPTED,
                provider_message_id=str(data["message_id"]),
                accepted_recipients=tuple(recipients),
            )
        return result(DeliveryOutcome.UNKNOWN, error_code="invalid_response")

    def get_updates(self, offset: int) -> list[dict]:
        try:
            response = self.client.post(
                f"https://api.telegram.org/bot{self.token}/getUpdates",
                json={"offset": offset, "timeout": 0, "limit": 50, "allowed_updates": ["message"]},
            )
            payload = response.json()
        except (httpx.HTTPError, ValueError):
            raise ProviderConfigurationError("Telegram polling failed") from None
        if (
            response.status_code != 200
            or not isinstance(payload, dict)
            or payload.get("ok") is not True
            or not isinstance(payload.get("result"), list)
        ):
            raise ProviderConfigurationError(
                "Telegram polling failed; check token/webhook settings"
            )
        return payload["result"]
