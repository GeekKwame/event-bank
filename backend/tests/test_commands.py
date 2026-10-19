from io import StringIO

import httpx
import pytest
from django.core.management import CommandError, call_command

from apps.events.models import Event
from apps.ingestion import services as ingestion_services
from apps.sources import services as source_services
from apps.sources.models import Source
from tests.conftest import EXAMPLE_SOURCE_URL, public_resolver

pytestmark = pytest.mark.django_db


@pytest.fixture
def mocked_network(monkeypatch, fake_site):
    original_register = source_services.register_source

    def register_with_mock_dns(*args, **kwargs):
        kwargs.setdefault("resolver", public_resolver)
        return original_register(*args, **kwargs)

    monkeypatch.setattr(
        "apps.sources.management.commands.add_source.register_source", register_with_mock_dns
    )
    original_ingest = ingestion_services.ingest_source

    def ingest_with_fake_site(source_id, **kwargs):
        return original_ingest(source_id, fetcher_factory=fake_site.fetcher_factory, **kwargs)

    monkeypatch.setattr(
        "apps.ingestion.management.commands.ingest_source.ingest_source", ingest_with_fake_site
    )
    return fake_site


def test_add_source_then_ingest(example_connector, mocked_network):
    out = StringIO()
    call_command("add_source", EXAMPLE_SOURCE_URL, "--approve", "--name", "Example", stdout=out)
    source = Source.objects.get()
    assert f"Registered source #{source.pk}" in out.getvalue()
    assert "status=approved" in out.getvalue()

    out = StringIO()
    call_command("ingest_source", str(source.pk), stdout=out)
    assert "created=2" in out.getvalue()
    assert Event.objects.count() == 2


def test_add_source_reports_validation_errors(example_connector, mocked_network):
    with pytest.raises(CommandError, match="unsupported_scheme"):
        call_command("add_source", "http://events.example.org/calendar")
    with pytest.raises(CommandError, match="ip_literal_not_allowed"):
        call_command("add_source", "https://169.254.169.254/")
    with pytest.raises(CommandError, match="invalid_config: expected KEY=VALUE"):
        call_command("add_source", EXAMPLE_SOURCE_URL, "--config", "novalue")
    with pytest.raises(CommandError, match="invalid_config: Example"):
        call_command("add_source", EXAMPLE_SOURCE_URL, "--config", "colour=blue")


def test_add_source_passes_config_to_luma(mocked_network, monkeypatch):
    monkeypatch.setenv("LUMA_API_KEY_ACCRA", "luma-test-key-0000000000")
    out = StringIO()
    call_command(
        "add_source",
        "https://lu.ma/example-tech",
        "--config",
        "api_key_env=LUMA_API_KEY_ACCRA",
        stdout=out,
    )
    source = Source.objects.get()
    assert source.connector == "luma"
    assert source.config == {"api_key_env": "LUMA_API_KEY_ACCRA"}

    with pytest.raises(CommandError, match="invalid_config"):
        call_command(
            "add_source", "https://lu.ma/other-cal", "--config", "include_listed_events=true"
        )


def test_ingest_source_errors(example_connector, mocked_network):
    with pytest.raises(CommandError, match="source_not_found"):
        call_command("ingest_source", "999")

    pending = Source.objects.create(url=EXAMPLE_SOURCE_URL, connector="example")
    with pytest.raises(CommandError, match="source_not_approved"):
        call_command("ingest_source", str(pending.pk))

    pending.review(Source.Status.APPROVED)
    mocked_network.response = httpx.Response(503)
    with pytest.raises(CommandError, match="http_status"):
        call_command("ingest_source", str(pending.pk))
