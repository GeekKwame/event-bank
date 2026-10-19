import logging
import os
import subprocess
import sys
from pathlib import Path

import pytest
from django.urls import URLPattern, URLResolver, get_resolver
from rest_framework.test import APIRequestFactory
from rest_framework.views import APIView

from apps.common.permissions import DenyAll
from apps.common.phone import InvalidPhoneNumber, normalize_ghana_mobile, to_ghana_national
from apps.common.redaction import RedactingFormatter, redact

BACKEND_DIR = Path(__file__).resolve().parent.parent


@pytest.mark.parametrize(
    "value",
    [
        "0241234567",
        "024 123 4567",
        "024-123-4567",
        "+233241234567",
        "233241234567",
        "00233241234567",
    ],
)
def test_normalizes_ghana_mobile_numbers_to_e164(value):
    assert normalize_ghana_mobile(value) == "+233241234567"


@pytest.mark.parametrize(
    "value",
    [
        "",
        "024123456",
        "02412345678",
        "0301234567",
        "+2348031234567",
        "+233 24 abc 4567",
        "٠٢٤١٢٣٤٥٦٧",
    ],
)
def test_rejects_invalid_ghana_mobile_numbers(value):
    with pytest.raises(InvalidPhoneNumber):
        normalize_ghana_mobile(value)


def test_converts_e164_to_national_format():
    assert to_ghana_national("+233541234567") == "0541234567"


def test_redact_removes_known_secrets_and_secret_query_params():
    text = "GET https://api.example.org/x?key=abc123&page=2 token=zzz with s3cr3t-value"
    assert redact(text, ["s3cr3t-value"]) == (
        "GET https://api.example.org/x?key=[REDACTED]&page=2 token=[REDACTED] with [REDACTED]"
    )


def test_redacting_formatter_covers_tracebacks(settings):
    settings.MNOTIFY_API_KEY = "super-secret-mnotify-key"
    formatter = RedactingFormatter("%(message)s")
    try:
        raise ValueError(
            "failed calling https://api.mnotify.com/api/sms/quick?key=super-secret-mnotify-key"
        )
    except ValueError:
        record = logging.LogRecord("x", logging.ERROR, __file__, 1, "boom", None, sys.exc_info())
    output = formatter.format(record)
    assert "super-secret-mnotify-key" not in output
    assert "[REDACTED]" in output


@pytest.mark.django_db
def test_health_is_public(client):
    response = client.get("/api/health/", secure=True)
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "ok"}


def _drf_views(patterns):
    for pattern in patterns:
        if isinstance(pattern, URLResolver):
            if pattern.app_name != "admin":
                yield from _drf_views(pattern.url_patterns)
        elif isinstance(pattern, URLPattern):
            view_class = getattr(pattern.callback, "cls", None)
            if view_class is not None and issubclass(view_class, APIView):
                yield view_class


def test_every_api_view_declares_its_permissions():
    views = list(_drf_views(get_resolver().url_patterns))
    assert views, "expected at least the health view"
    for view_class in views:
        assert DenyAll not in view_class.permission_classes, (
            f"{view_class.__name__} must declare permission_classes explicitly"
        )


def test_views_without_declared_permissions_are_denied():
    class Undeclared(APIView):
        def get(self, request):
            return None

    response = Undeclared.as_view()(APIRequestFactory().get("/"))
    assert response.status_code in (401, 403)


def _import_settings(**env):
    base = {k: v for k, v in os.environ.items() if not k.startswith("DJANGO_")}
    return subprocess.run(  # noqa: S603
        [sys.executable, "-c", "import config.settings"],
        cwd=BACKEND_DIR,
        env={**base, **env},
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize(
    "env",
    [
        {},
        {"DJANGO_SECRET_KEY": "change-me"},
        {"DJANGO_SECRET_KEY": "dev-insecure-secret-key"},
        {"DJANGO_SECRET_KEY": "short-but-not-a-placeholder"},
        {"DJANGO_SECRET_KEY": "x" * 60, "DJANGO_ALLOWED_HOSTS": ""},
    ],
)
def test_production_settings_refuse_unsafe_configuration(env):
    result = _import_settings(**env)
    assert result.returncode != 0
    assert "ImproperlyConfigured" in result.stderr


def test_production_settings_accept_safe_configuration():
    result = _import_settings(DJANGO_SECRET_KEY="x" * 60, DJANGO_ALLOWED_HOSTS="events.example.org")
    assert result.returncode == 0, result.stderr


def test_debug_mode_allows_development_key():
    result = _import_settings(DJANGO_DEBUG="true", DJANGO_SECRET_KEY="dev-insecure-secret-key")
    assert result.returncode == 0, result.stderr
