import pytest

from apps.sources.models import Source
from apps.sources.services import SourceRegistrationError, register_source
from tests.conftest import EXAMPLE_SOURCE_URL, public_resolver

pytestmark = pytest.mark.django_db


def test_registers_supported_url_as_pending_with_canonical_url(example_connector):
    source = register_source(
        "https://Events.Example.org/calendar#upcoming", resolver=public_resolver
    )
    assert source.url == EXAMPLE_SOURCE_URL
    assert source.connector == "example"
    assert source.status == Source.Status.PENDING
    assert source.timezone == "Africa/Accra"


def test_approve_flag_marks_source_reviewed(example_connector):
    source = register_source(EXAMPLE_SOURCE_URL, approve=True, resolver=public_resolver)
    assert source.status == Source.Status.APPROVED
    assert source.reviewed_at is not None


@pytest.mark.parametrize(
    ("url", "code"),
    [
        ("http://events.example.org/calendar", "unsupported_scheme"),
        ("https://unsupported.example.com/", "unsupported_source"),
        ("https://localhost/", "unsupported_source"),
        ("https://169.254.169.254/latest/meta-data/", "ip_literal_not_allowed"),
        ("https://192.168.0.10/", "ip_literal_not_allowed"),
        ("https://user:pw@events.example.org/", "credentials_in_url"),
    ],
)
def test_rejects_unsupported_and_unsafe_urls(example_connector, url, code):
    with pytest.raises(SourceRegistrationError) as excinfo:
        register_source(url, resolver=public_resolver)
    assert excinfo.value.code == code
    assert not Source.objects.exists()


def test_rejects_hostnames_that_resolve_to_private_addresses(example_connector):
    with pytest.raises(SourceRegistrationError) as excinfo:
        register_source(EXAMPLE_SOURCE_URL, resolver=lambda host, port: ["10.0.0.8"])
    assert excinfo.value.code == "non_public_address"


def test_rejects_duplicates_and_bad_timezones(example_connector):
    register_source(EXAMPLE_SOURCE_URL, resolver=public_resolver)
    with pytest.raises(SourceRegistrationError) as excinfo:
        register_source(EXAMPLE_SOURCE_URL + "#again", resolver=public_resolver)
    assert excinfo.value.code == "duplicate_source"

    with pytest.raises(SourceRegistrationError) as excinfo:
        register_source(
            "https://events.example.org/other", timezone="Mars/Base", resolver=public_resolver
        )
    assert excinfo.value.code == "invalid_timezone"
