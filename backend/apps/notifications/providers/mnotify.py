"""mNotify BMS API v2 quick SMS: https://readthedocs.mnotify.com/"""

from collections.abc import Sequence

import httpx
from django.conf import settings

from apps.common.phone import InvalidPhoneNumber, normalize_ghana_mobile, to_ghana_national
from apps.notifications.providers.base import (
    DeliveryOutcome,
    NotificationProvider,
    ProviderConfigurationError,
    SendingDisabledError,
    SendResult,
)

MNOTIFY_QUICK_SMS_URL = "https://api.mnotify.com/api/sms/quick"
MNOTIFY_SUCCESS_CODE = "2000"
MAX_SENDER_ID_LENGTH = 11

# Documented response codes. 405 is "Too Many Requests"; codes we do not
# recognise are treated as UNKNOWN rather than guessed at.
_REJECTED_CODES = frozenset(
    {"400", "401", "402", "403", "404"} | {str(c) for c in range(4000, 4016)}
)
_RETRYABLE_CODES = frozenset({"405"})
# The connection was never established, so the request cannot have been processed.
_NOT_SENT_ERRORS = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)


class MNotifyProvider(NotificationProvider):
    name = "mnotify"
    channel = "sms"

    def __init__(
        self,
        api_key: str | None = None,
        sender_id: str | None = None,
        client: httpx.Client | None = None,
    ):
        self.api_key = api_key if api_key is not None else settings.MNOTIFY_API_KEY
        self.sender_id = sender_id if sender_id is not None else settings.MNOTIFY_SENDER_ID
        if not self.api_key:
            raise ProviderConfigurationError("MNOTIFY_API_KEY is not configured")
        if not self.sender_id or len(self.sender_id) > MAX_SENDER_ID_LENGTH:
            raise ProviderConfigurationError(
                f"MNOTIFY_SENDER_ID must be 1-{MAX_SENDER_ID_LENGTH} characters"
            )
        if client is None and not settings.SMS_SENDING_ENABLED:
            raise SendingDisabledError("Real SMS sending is disabled (SMS_SENDING_ENABLED=false)")
        self.client = client or httpx.Client(timeout=15, trust_env=False)

    def _result(self, outcome: DeliveryOutcome, **kwargs) -> SendResult:
        return SendResult(provider=self.name, outcome=outcome, **kwargs)

    def send(self, recipients: Sequence[str], message: str) -> SendResult:
        if not recipients or not message.strip():
            return self._result(
                DeliveryOutcome.REJECTED,
                error_code="invalid_request",
                error_message="A recipient and a message are required",
            )
        try:
            national = [to_ghana_national(recipient) for recipient in recipients]
        except InvalidPhoneNumber:
            return self._result(
                DeliveryOutcome.REJECTED,
                error_code="invalid_recipient",
                error_message="Recipients must be Ghana mobile numbers",
            )

        # Error messages below never include the request URL: it carries the API key.
        try:
            response = self.client.post(
                MNOTIFY_QUICK_SMS_URL,
                params={"key": self.api_key},
                json={
                    "recipient": national,
                    "sender": self.sender_id,
                    "message": message,
                    "is_schedule": False,
                    "schedule_date": "",
                },
            )
        except _NOT_SENT_ERRORS as exc:
            return self._result(
                DeliveryOutcome.RETRYABLE,
                error_code="connect_failed",
                error_message=type(exc).__name__,
            )
        except httpx.HTTPError as exc:
            return self._result(
                DeliveryOutcome.UNKNOWN,
                error_code="transport_error",
                error_message=type(exc).__name__,
            )

        if response.status_code == 429:
            return self._result(DeliveryOutcome.RETRYABLE, error_code="http_429")
        if response.status_code >= 500:
            return self._result(DeliveryOutcome.UNKNOWN, error_code=f"http_{response.status_code}")
        if response.status_code >= 400:
            return self._result(DeliveryOutcome.REJECTED, error_code=f"http_{response.status_code}")

        try:
            payload = response.json()
        except ValueError:
            return self._result(DeliveryOutcome.UNKNOWN, error_code="invalid_response")
        if not isinstance(payload, dict):
            return self._result(DeliveryOutcome.UNKNOWN, error_code="invalid_response")

        code = str(payload.get("code", ""))
        provider_message = str(payload.get("message", ""))[:500]
        if code == MNOTIFY_SUCCESS_CODE:
            summary = payload.get("summary") or {}
            return self._result(
                DeliveryOutcome.ACCEPTED,
                provider_message_id=summary.get("_id"),
                accepted_recipients=tuple(
                    _to_e164(number) for number in summary.get("numbers_sent") or []
                ),
                raw=payload,
            )
        if code in _RETRYABLE_CODES:
            outcome = DeliveryOutcome.RETRYABLE
        elif code in _REJECTED_CODES:
            outcome = DeliveryOutcome.REJECTED
        else:
            outcome = DeliveryOutcome.UNKNOWN
        return self._result(
            outcome,
            error_code=f"mnotify_{code or 'missing'}",
            error_message=provider_message,
            raw=payload,
        )


def _to_e164(number: str) -> str:
    try:
        return normalize_ghana_mobile(str(number))
    except InvalidPhoneNumber:
        return str(number)
