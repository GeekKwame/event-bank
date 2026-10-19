import pytest

from connectors import registry
from tests.conftest import ExampleConnector


def test_match_connector_by_allowlisted_https_host(example_connector):
    assert registry.match_connector("https://events.example.org/calendar") is ExampleConnector
    for url in ["http://events.example.org/", "https://other.example.org/"]:
        with pytest.raises(registry.UnsupportedSourceError):
            registry.match_connector(url)


def test_registry_rejects_duplicate_slugs(example_connector):
    class Duplicate(ExampleConnector):
        pass

    with pytest.raises(ValueError):
        registry.register(Duplicate)
    assert registry.get_connector("example") is ExampleConnector


def test_ambiguous_urls_are_rejected(example_connector):
    class Overlapping(ExampleConnector):
        slug = "overlapping"

    registry.register(Overlapping)
    with pytest.raises(registry.UnsupportedSourceError, match="more than one"):
        registry.match_connector("https://events.example.org/calendar")


def test_canonicalize_url_drops_fragment_and_default_port():
    assert (
        ExampleConnector.canonicalize_url("https://Events.Example.org:443/e/1?a=1#top")
        == "https://events.example.org/e/1?a=1"
    )
