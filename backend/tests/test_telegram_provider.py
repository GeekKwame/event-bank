import logging
import sys

import httpx
import pytest

from apps.common.redaction import RedactingFormatter
from apps.notifications.providers.base import (
    DeliveryOutcome,
    ProviderConfigurationError,
    SendingDisabledError,
)
from apps.notifications.providers.telegram import TelegramProvider

TOKEN = "123456789:test-token-0000000000000000000000000"


def provider(handler):
    return TelegramProvider(
        token=TOKEN, client=httpx.Client(transport=httpx.MockTransport(handler))
    )


def test_telegram_plain_text_no_paid_broadcasts():
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 42}})

    bot = provider(handle)
    result = bot.send(["12345"], "Event <title> & link")
    assert result.outcome == DeliveryOutcome.ACCEPTED
    assert result.provider_message_id == "42"
    assert result.accepted_recipients == ("12345",)
    assert requests[0].url.host == "api.telegram.org"
    assert b'"allow_paid_broadcast":false' in requests[0].content
    assert b"parse_mode" not in requests[0].content
    bot.close()


@pytest.mark.parametrize(
    ("status", "payload", "outcome"),
    [
        (400, {"ok": False, "error_code": 400}, DeliveryOutcome.REJECTED),
        (403, {"ok": False, "error_code": 403}, DeliveryOutcome.REJECTED),
        (429, {"ok": False, "parameters": {"retry_after": 90}}, DeliveryOutcome.RETRYABLE),
        (500, {}, DeliveryOutcome.UNKNOWN),
        (200, {"ok": True, "result": {}}, DeliveryOutcome.UNKNOWN),
        (200, [], DeliveryOutcome.UNKNOWN),
        (302, {}, DeliveryOutcome.UNKNOWN),
    ],
)
def test_http_outcomes(status, payload, outcome):
    bot = provider(lambda request: httpx.Response(status, json=payload))
    result = bot.send(["12345"], "Example")
    assert result.outcome == outcome
    if status == 429:
        assert result.retry_after == 90
    assert result.raw == {}
    bot.close()


@pytest.mark.parametrize(
    ("exception", "expected"),
    [
        (httpx.ConnectTimeout, DeliveryOutcome.RETRYABLE),
        (httpx.ReadTimeout, DeliveryOutcome.UNKNOWN),
    ],
)
def test_transport_errors_do_not_leak_token(exception, expected):
    def handle(request):
        raise exception(str(request.url))

    bot = provider(handle)
    result = bot.send(["12345"], "Example")
    assert result.outcome == expected
    assert TOKEN not in repr(result)
    with pytest.raises(ProviderConfigurationError) as exc:
        bot.get_updates(0)
    assert TOKEN not in str(exc.value)
    bot.close()


def test_invalid_requests_never_send():
    def forbidden(request):
        pytest.fail("Invalid requests must not reach transport")

    bot = provider(forbidden)
    for recipients, text in [
        ([], "x"),
        (["@channel"], "x"),
        (["123", "456"], "x"),
        (["123"], "🙂" * 3000),
    ]:
        assert bot.send(recipients, text).outcome == DeliveryOutcome.REJECTED
    bot.close()


def test_disabled_by_default_and_bad_token(settings):
    settings.TELEGRAM_SENDING_ENABLED = False
    with pytest.raises(SendingDisabledError):
        TelegramProvider(token=TOKEN)
    with pytest.raises(ProviderConfigurationError):
        TelegramProvider(token="malformed/token")


def test_polling_offset_and_validation():
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={"ok": True, "result": [{"update_id": 91}]})

    bot = provider(handle)
    assert bot.get_updates(90) == [{"update_id": 91}]
    assert b'"offset":90' in requests[0].content
    assert b'"allowed_updates":["message"]' in requests[0].content
    bot.close()


def test_log_tracebacks_redact_telegram_token(settings):
    settings.TELEGRAM_BOT_TOKEN = TOKEN
    try:
        raise ValueError(f"https://api.telegram.org/bot{TOKEN}/sendMessage")
    except ValueError:
        record = logging.LogRecord("x", logging.ERROR, __file__, 1, "failed", None, sys.exc_info())
    assert TOKEN not in RedactingFormatter("%(message)s").format(record)
