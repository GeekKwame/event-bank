from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import pytest

from apps.events.models import Event, EventRevision
from apps.ingestion.models import SourceRun
from apps.ingestion.services import ingest_source
from apps.sources.models import Source
from apps.sources.services import SourceRegistrationError, fetch_policy_for, register_source
from connectors import registry
from connectors.base import AttendanceMode, ConnectorError, EventStatus, SourceSpec
from connectors.http import SafeFetcher
from connectors.platforms.luma import LumaCalendarConnector
from connectors.platforms.luma_ical import LumaICalConnector
from tests.conftest import public_resolver

FEED = (Path(__file__).parent / "fixtures" / "luma_ical" / "calendar.ics").read_text()
FEED_URL = "https://api.lu.ma/ics/get?entity=calendar&id=cal-Synthetic0001"
NOW = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)


class FakeFeed:
    def __init__(self):
        self.body = FEED
        self.content_type = "text/calendar; charset=utf-8"
        self.requests: list[httpx.Request] = []

    def handler(self, request):
        self.requests.append(request)
        return httpx.Response(
            200, content=self.body.encode(), headers={"Content-Type": self.content_type}
        )

    def fetcher_factory(self, policy):
        return SafeFetcher(
            policy, resolver=public_resolver, transport=httpx.MockTransport(self.handler)
        )


@pytest.fixture
def feed(monkeypatch):
    monkeypatch.setattr(LumaICalConnector, "clock", staticmethod(lambda: NOW))
    return FakeFeed()


def collect(feed):
    spec = SourceSpec(url=FEED_URL, timezone="Africa/Accra")
    with feed.fetcher_factory(fetch_policy_for(LumaICalConnector)) as fetcher:
        return {event.external_id: event for event in LumaICalConnector().collect(spec, fetcher)}


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (FEED_URL, True),
        ("https://api2.luma.com/ics/get?entity=calendar&id=cal-Synthetic0001", True),
        ("https://api.lu.ma/ics/get?entity=discover&id=discplace-Synthetic01", True),
        ("https://api.lu.ma/ics/get?entity=user&id=usr-Synthetic01", False),
        ("https://api.lu.ma/ics/get?entity=calendar&id=cal-Synthetic0001&token=secret", False),
        ("https://api.lu.ma/ics/get?entity=calendar&id=cal-a&id=cal-b", False),
        ("https://api.lu.ma/ics/other?entity=calendar&id=cal-Synthetic0001", False),
        ("http://api.lu.ma/ics/get?entity=calendar&id=cal-Synthetic0001", False),
        ("https://evil.example.com/ics/get?entity=calendar&id=cal-Synthetic0001", False),
        ("https://api.lu.ma/ics/get?entity=calendar&id=cal-../../x", False),
    ],
)
def test_matches_only_public_calendar_and_city_feeds(url, expected):
    assert LumaICalConnector.matches(url) is expected


def test_registry_routes_feeds_and_calendar_pages_to_different_connectors():
    assert registry.match_connector(FEED_URL) is LumaICalConnector
    assert registry.match_connector("https://luma.com/example-tech") is LumaCalendarConnector


def test_canonicalize_url():
    assert (
        LumaICalConnector.canonicalize_url(
            "https://API.lu.ma/ics/get?id=cal-Synthetic0001&entity=calendar"
        )
        == FEED_URL
    )


def test_maps_public_upcoming_events(feed):
    events = collect(feed)

    assert sorted(events) == [
        "evt-synthetic1001@example.org",
        "evt-synthetic1002@example.org",
        "evt-synthetic1003@example.org",
    ]
    meetup = events["evt-synthetic1001@example.org"]
    assert meetup.title == "Example Developer Meetup"
    assert meetup.url == "https://luma.com/example-ical-meetup"
    assert meetup.starts_at == datetime(2026, 11, 14, 16, tzinfo=meetup.starts_at.tzinfo)
    assert meetup.timezone == "Africa/Accra"
    assert meetup.address == "Example Innovation Hub, 1 Example Street, Accra"
    assert meetup.attendance_mode is AttendanceMode.OFFLINE
    assert "A day of talks and demos." in meetup.description

    workshop = events["evt-synthetic1002@example.org"]
    assert workshop.attendance_mode is AttendanceMode.ONLINE
    assert workshop.address is None
    assert workshop.ends_at == datetime(2026, 12, 2, 18, 30, tzinfo=UTC)

    hackathon = events["evt-synthetic1003@example.org"]
    assert hackathon.status is EventStatus.CANCELLED
    assert hackathon.starts_at == date(2026, 12, 5)


def test_never_exposes_private_details(feed):
    dumped = repr(list(collect(feed).values()))
    for private in (
        "zoom.example.org",
        "Example Person",
        "person@example.org",
        "attendee@example.org",
        "Private Planning",
    ):
        assert private not in dumped


@pytest.mark.parametrize("body", ["<html>not a calendar</html>", "BEGIN:VEVENT\nEND:VEVENT\n"])
def test_rejects_invalid_feeds(feed, body):
    feed.body = body
    with pytest.raises(ConnectorError) as excinfo:
        collect(feed)
    assert excinfo.value.code == "unexpected_response"


@pytest.mark.django_db
class TestLumaICalIngestion:
    @pytest.fixture
    def source(self):
        return Source.objects.create(
            url=FEED_URL, connector="luma-ical", status=Source.Status.APPROVED
        )

    def ingest(self, source, feed):
        return ingest_source(source.pk, fetcher_factory=feed.fetcher_factory, environ={})

    def test_ingests_idempotently_and_detects_cancellation(self, source, feed):
        first = self.ingest(source, feed)
        assert first.status == SourceRun.Status.SUCCEEDED, first.error_message
        assert first.events_created == 3
        meetup = Event.objects.get(external_id="evt-synthetic1001@example.org")
        assert meetup.starts_at == datetime(2026, 11, 14, 16, tzinfo=UTC)
        assert meetup.organizer_name == ""

        second = self.ingest(source, feed)
        assert (second.events_created, second.events_updated, second.events_unchanged) == (0, 0, 3)

        feed.body = FEED.replace(
            "SUMMARY:Example Developer Meetup", "SUMMARY:Example Developer Meetup\nSTATUS:CANCELLED"
        )
        third = self.ingest(source, feed)
        assert third.events_updated == 1
        revision = EventRevision.objects.get(kind="updated")
        assert revision.changed_fields == ["status"]
        assert revision.is_significant

    def test_rejects_non_calendar_content_type(self, source, feed):
        feed.content_type = "text/html"
        run = self.ingest(source, feed)
        assert run.status == SourceRun.Status.FAILED
        assert run.error_code == "unexpected_content"
        assert not Event.objects.exists()


@pytest.mark.django_db
def test_register_feed_without_credentials():
    source = register_source(
        "https://api.lu.ma/ics/get?id=cal-Synthetic0001&entity=calendar",
        resolver=public_resolver,
        environ={},
    )
    assert source.connector == "luma-ical"
    assert source.url == FEED_URL
    assert source.config == {}

    with pytest.raises(SourceRegistrationError) as excinfo:
        register_source(
            "https://api.lu.ma/ics/get?entity=user&id=usr-Synthetic01",
            resolver=public_resolver,
            environ={},
        )
    assert excinfo.value.code == "unsupported_source"
