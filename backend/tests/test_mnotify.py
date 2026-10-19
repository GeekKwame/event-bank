import json
import logging

import httpx
import pytest

from apps.notifications.providers.base import (
    DeliveryOutcome,
    ProviderConfigurationError,
    SendingDisabledError,
)
from apps.notifications.providers.console import ConsoleSMSProvider
from apps.notifications.providers.mnotify import MNotifyProvider

API_KEY = "mnotify-test-key-0000000000"


def provider(handler):
    return MNotifyProvider(
        api_key=API_KEY,
        sender_id="EventBank",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def reply(code, **extra):
    return lambda request: httpx.Response(200, json={"code": code, "message": "m", **extra})


def test_sends_national_numbers_and_maps_accepted_back_to_e164():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "status": "success",
                "code": "2000",
                "message": "messages sent successfully",
                "summary": {"_id": "abc-123", "numbers_sent": ["0241234567"]},
            },
        )

    result = provider(handler).send(["+233241234567"], "Hello")

    assert seen[0].url.params["key"] == API_KEY
    assert json.loads(seen[0].content) == {
        "recipient": ["0241234567"],
        "sender": "EventBank",
        "message": "Hello",
        "is_schedule": False,
        "schedule_date": "",
    }
    assert result.outcome is DeliveryOutcome.ACCEPTED
    assert result.provider_message_id == "abc-123"
    assert result.accepted_recipients == ("+233241234567",)


@pytest.mark.parametrize(
    ("handler", "outcome"),
    [
        (reply("4004"), DeliveryOutcome.REJECTED),
        (reply("4014"), DeliveryOutcome.REJECTED),
        (reply("401"), DeliveryOutcome.REJECTED),
        (reply("405"), DeliveryOutcome.RETRYABLE),
        (reply("9999"), DeliveryOutcome.UNKNOWN),
        (lambda request: httpx.Response(429), DeliveryOutcome.RETRYABLE),
        (lambda request: httpx.Response(401), DeliveryOutcome.REJECTED),
        (lambda request: httpx.Response(500), DeliveryOutcome.UNKNOWN),
        (lambda request: httpx.Response(502), DeliveryOutcome.UNKNOWN),
        (lambda request: httpx.Response(200, text="<html>oops</html>"), DeliveryOutcome.UNKNOWN),
        (lambda request: httpx.Response(200, json=["unexpected"]), DeliveryOutcome.UNKNOWN),
    ],
)
def test_classifies_provider_responses(handler, outcome):
    assert provider(handler).send(["0241234567"], "Hi").outcome is outcome


@pytest.mark.parametrize(
    ("exc", "outcome"),
    [
        (httpx.ConnectError("refused"), DeliveryOutcome.RETRYABLE),
        (httpx.ConnectTimeout("slow"), DeliveryOutcome.RETRYABLE),
        (httpx.ReadTimeout("slow"), DeliveryOutcome.UNKNOWN),
        (httpx.WriteTimeout("slow"), DeliveryOutcome.UNKNOWN),
        (httpx.RemoteProtocolError("closed"), DeliveryOutcome.UNKNOWN),
    ],
)
def test_transport_failures_distinguish_not_sent_from_unknown(exc, outcome):
    def handler(request):
        raise exc

    result = provider(handler).send(["0241234567"], "Hi")
    assert result.outcome is outcome


def test_unknown_outcomes_are_never_retried_automatically():
    assert DeliveryOutcome.RETRYABLE.may_retry_automatically
    for outcome in (DeliveryOutcome.UNKNOWN, DeliveryOutcome.REJECTED, DeliveryOutcome.ACCEPTED):
        assert not outcome.may_retry_automatically


def test_errors_never_contain_the_api_key(caplog):
    def handler(request):
        raise httpx.ReadTimeout(f"timed out calling {request.url}")

    with caplog.at_level(logging.DEBUG):
        result = provider(handler).send(["0241234567"], "Hi")

    assert API_KEY not in repr(result)
    assert API_KEY not in caplog.text


@pytest.mark.parametrize("recipients", [["+2348031234567"], ["0301234567"], ["not a number"]])
def test_rejects_non_ghana_mobile_recipients_without_calling_api(recipients):
    def handler(request):
        raise AssertionError("must not be called")

    result = provider(handler).send(recipients, "Hi")
    assert result.outcome is DeliveryOutcome.REJECTED
    assert result.error_code == "invalid_recipient"


def test_configuration_is_validated():
    with pytest.raises(ProviderConfigurationError):
        MNotifyProvider(api_key="", sender_id="EventBank", client=httpx.Client())
    with pytest.raises(ProviderConfigurationError):
        MNotifyProvider(api_key=API_KEY, sender_id="TwelveChars!", client=httpx.Client())


def test_real_sending_is_disabled_by_default(settings):
    settings.SMS_SENDING_ENABLED = False
    with pytest.raises(SendingDisabledError):
        MNotifyProvider(api_key=API_KEY, sender_id="EventBank")


def test_console_provider_masks_numbers(caplog):
    with caplog.at_level(logging.INFO):
        result = ConsoleSMSProvider().send(["+233241234567"], "Hello")
    assert result.outcome is DeliveryOutcome.ACCEPTED
    assert "+233241234567" not in caplog.text
    assert "+23324****567" in caplog.text
