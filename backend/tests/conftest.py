import copy
import json
import socket

import httpx
import pytest

from connectors import registry
from connectors.base import Connector
from connectors.extract.jsonld import find_events, text_value, to_raw_event
from connectors.http import SafeFetcher

PUBLIC_IP = "93.184.215.14"
EXAMPLE_HOST = "events.example.org"
EXAMPLE_SOURCE_URL = f"https://{EXAMPLE_HOST}/calendar"


class NetworkAccessError(AssertionError):
    pass


@pytest.fixture(autouse=True)
def block_network(monkeypatch):
    """Fail any test that tries to resolve a hostname or open a socket.

    PostgreSQL is unaffected: psycopg connects through libpq, not Python sockets.
    """

    def deny(*args, **kwargs):
        raise NetworkAccessError(f"Tests must not use the network (attempted {args[:2]!r})")

    monkeypatch.setattr(socket, "getaddrinfo", deny)
    monkeypatch.setattr(socket, "create_connection", deny)
    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket.socket, "connect_ex", deny)


def public_resolver(host, port):
    return [PUBLIC_IP]


class ExampleConnector(Connector):
    """Test-only connector for the synthetic events.example.org pages."""

    slug = "example"
    name = "Example (tests only)"
    allowed_hosts = frozenset({EXAMPLE_HOST})

    def collect(self, source, fetcher):
        page = fetcher.get(source.url)
        for node in find_events(page.text):
            external_id = text_value(node.get("@id")) or ""
            yield to_raw_event(node, external_id=external_id, base_url=page.url)


@pytest.fixture
def example_connector(monkeypatch):
    monkeypatch.setattr(registry, "_REGISTRY", {})
    monkeypatch.setattr(registry, "_discovered", True)
    registry.register(ExampleConnector)
    return ExampleConnector


BASE_EVENTS = [
    {
        "@type": "Event",
        "@id": "evt-meetup",
        "name": "Example Tech Meetup",
        "url": "https://events.example.org/e/meetup?utm_source=feed",
        "startDate": "2026-11-14T09:00:00+00:00",
        "endDate": "2026-11-14T17:00:00+00:00",
        "eventStatus": "https://schema.org/EventScheduled",
        "eventAttendanceMode": "https://schema.org/OfflineEventAttendanceMode",
        "location": {
            "@type": "Place",
            "name": "Example Hall",
            "address": {
                "@type": "PostalAddress",
                "streetAddress": "1 Example Street",
                "addressLocality": "Accra",
                "addressCountry": "GH",
            },
        },
        "organizer": {"@type": "Organization", "name": "Example Community"},
        "description": "A day of talks.",
    },
    {
        "@type": "EducationEvent",
        "@id": "evt-workshop",
        "name": "Example Online Workshop",
        "url": "/e/workshop",
        "startDate": "2026-12-01T18:30:00+01:00",
        "location": {"@type": "VirtualLocation", "url": "https://meet.example.org/abc"},
        "description": "Hands-on session.",
    },
]


def jsonld_page(events) -> str:
    graph = [{"@type": "Organization", "name": "Example Community"}, *events]
    data = json.dumps({"@context": "https://schema.org", "@graph": graph})
    return (
        "<!doctype html><html><head><title>Example</title>"
        f'<script type="application/ld+json">{data}</script>'
        '<script type="application/ld+json">{not valid json</script>'
        "</head><body></body></html>"
    )


@pytest.fixture
def base_events():
    return copy.deepcopy(BASE_EVENTS)


class FakeSite:
    """Serves configurable responses through httpx.MockTransport and records requests."""

    def __init__(self):
        self.html = jsonld_page(BASE_EVENTS)
        self.response = None
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.response is not None:
            return self.response(request) if callable(self.response) else self.response
        return httpx.Response(200, html=self.html)

    def fetcher_factory(self, policy):
        return SafeFetcher(
            policy, resolver=public_resolver, transport=httpx.MockTransport(self.handler)
        )


@pytest.fixture
def fake_site():
    return FakeSite()
