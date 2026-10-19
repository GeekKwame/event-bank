import copy
import json
import logging
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from apps.events.models import Event
from apps.ingestion.models import SourceRun
from apps.ingestion.services import ingest_source
from apps.sources.models import Source
from apps.sources.services import SourceRegistrationError, register_source
from connectors import registry
from connectors.base import AttendanceMode, ConnectorError, SourceSpec
from connectors.http import FetchPolicy, SafeFetcher
from connectors.platforms import luma
from connectors.platforms.luma import LumaCalendarConnector
from tests.conftest import PUBLIC_IP, public_resolver

FIXTURES = Path(__file__).parent / "fixtures" / "luma"
API_KEY = "luma-test-key-0000000000"
SOURCE_URL = "https://luma.com/example-tech"
NOW = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)


def load(name):
    return json.loads((FIXTURES / name).read_text())


class FakeLumaAPI:
    def __init__(self):
        self.calendar = load("calendar.json")
        self.pages = {None: load("events_page_1.json"), "cursor-page-2": load("events_page_2.json")}
        self.override = None
        self.requests: list[httpx.Request] = []

    def handler(self, request):
        self.requests.append(request)
        if self.override is not None:
            return self.override(request) if callable(self.override) else self.override
        if request.headers.get("x-luma-api-key") != API_KEY:
            return httpx.Response(401, json={"message": "Unauthorized"})
        if request.url.path == "/v1/calendars/get":
            return httpx.Response(200, json=self.calendar)
        if request.url.path == "/v1/calendars/events/list":
            return httpx.Response(200, json=self.pages[request.url.params.get("pagination_cursor")])
        return httpx.Response(404)

    def fetcher_factory(self, policy):
        return SafeFetcher(
            policy, resolver=public_resolver, transport=httpx.MockTransport(self.handler)
        )


@pytest.fixture
def api(monkeypatch):
    monkeypatch.setattr(LumaCalendarConnector, "clock", staticmethod(lambda: NOW))
    return FakeLumaAPI()


def collect(api, config=None, credentials=None):
    spec = SourceSpec(
        url=SOURCE_URL,
        timezone="Africa/Accra",
        config=config or {},
        credentials={"api_key": API_KEY} if credentials is None else credentials,
    )
    fetcher = api.fetcher_factory(FetchPolicy(allowed_hosts=frozenset({luma.API_HOST})))
    with fetcher:
        return list(LumaCalendarConnector().collect(spec, fetcher))


def test_registered_with_the_connector_registry():
    assert registry.get_connector("luma") is LumaCalendarConnector


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://luma.com/example-tech", True),
        ("https://lu.ma/example-tech", True),
        ("https://www.luma.com/example-tech/", True),
        ("https://luma.com/", False),
        ("https://luma.com/a/b", False),
        ("http://luma.com/example-tech", False),
        ("https://public-api.luma.com/v1/calendars/get", False),
        ("https://evil.example.com/luma.com", False),
    ],
)
def test_matches_calendar_urls(url, expected):
    assert LumaCalendarConnector.matches(url) is expected


def test_canonicalizes_to_luma_com():
    assert (
        LumaCalendarConnector.canonicalize_url("https://lu.ma/Example-Tech/?ref=x#y")
        == "https://luma.com/example-tech"
    )


def test_validate_config():
    assert LumaCalendarConnector.validate_config({"api_key_env": "LUMA_API_KEY_ACCRA"}) == {
        "api_key_env": "LUMA_API_KEY_ACCRA"
    }
    for bad in (
        {"api_key_env": "DJANGO_SECRET_KEY"},
        {"api_key_env": "LUMA_API_KEYX"},
        {"include_listed_events": "true"},
        {"api_key": "inline-secret"},
    ):
        with pytest.raises(ConnectorError) as excinfo:
            LumaCalendarConnector.validate_config(bad)
        assert excinfo.value.code == "invalid_config"


def test_credentials_come_from_the_environment():
    assert LumaCalendarConnector.credentials_from_env({}, {"LUMA_API_KEY": API_KEY}) == {
        "api_key": API_KEY
    }
    assert LumaCalendarConnector.credentials_from_env(
        {"api_key_env": "LUMA_API_KEY_ACCRA"}, {"LUMA_API_KEY_ACCRA": API_KEY}
    ) == {"api_key": API_KEY}
    with pytest.raises(ConnectorError) as excinfo:
        LumaCalendarConnector.credentials_from_env({}, {})
    assert excinfo.value.code == "missing_credentials"


def test_collect_maps_public_events_and_paginates(api):
    events = {event.external_id: event for event in collect(api)}

    assert sorted(events) == [
        "evt-synthetic0001",
        "evt-synthetic0002",
        "evt-synthetic0004",
        "evt-synthetic0005",
    ]
    meetup = events["evt-synthetic0001"]
    assert meetup.title == "Example Developer Meetup"
    assert meetup.url == "https://luma.com/example-dev-meetup"
    assert meetup.starts_at == datetime(2026, 11, 14, 16, tzinfo=UTC)
    assert meetup.ends_at == datetime(2026, 11, 14, 19, tzinfo=UTC)
    assert meetup.timezone == "Africa/Accra"
    assert meetup.venue_name == "Example Innovation Hub"
    assert meetup.address == "1 Example Street, Accra, Ghana"
    assert meetup.attendance_mode is AttendanceMode.OFFLINE
    assert meetup.organizer_name == "Example Tech Community"
    assert meetup.image_url == "https://images.example.org/covers/meetup.png"
    assert events["evt-synthetic0004"].attendance_mode is AttendanceMode.ONLINE
    assert events["evt-synthetic0005"].url == "https://luma.com/example-hackathon"


def test_never_exposes_private_details(api):
    events = {event.external_id: event for event in collect(api)}

    assert "evt-synthetic0003" not in events
    breakfast = events["evt-synthetic0002"]
    assert breakfast.venue_name is None
    assert breakfast.address == "Accra, Greater Accra"
    dumped = repr(list(events.values()))
    for private in ("99 Hidden Lane", "Private Residence", "organiser@example.org", "zoom.example"):
        assert private not in dumped


def test_requests_use_the_api_key_header_and_expected_query(api):
    collect(api)

    paths = [request.url.path for request in api.requests]
    assert paths == ["/v1/calendars/get", "/v1/calendars/events/list", "/v1/calendars/events/list"]
    for request in api.requests:
        assert request.url.host == PUBLIC_IP
        assert request.headers["Host"] == luma.API_HOST
        assert request.headers["x-luma-api-key"] == API_KEY
        assert API_KEY not in str(request.url)
    first_page = api.requests[1].url.params
    assert first_page["after"] == "2026-10-18T12:00:00.000Z"
    assert first_page.get_list("access") == ["manage"]
    assert "pagination_cursor" not in first_page
    assert api.requests[2].url.params["pagination_cursor"] == "cursor-page-2"


def test_events_managed_by_other_hosts_are_never_collected(api):
    listed = copy.deepcopy(api.pages[None]["entries"][0])
    listed.update(id="evt-synthetic0099", access="view")
    api.pages[None]["entries"].append(listed)

    events = collect(api)

    assert "evt-synthetic0099" not in {event.external_id for event in events}
    for request in api.requests[1:]:
        assert request.url.params.get_list("access") == ["manage"]


@pytest.mark.parametrize("is_personal", [True, None])
def test_personal_calendar_names_are_not_stored_as_organiser(api, is_personal):
    if is_personal is None:
        del api.calendar["is_personal"]
    else:
        api.calendar["is_personal"] = is_personal

    events = collect(api)

    assert events
    assert all(event.organizer_name is None for event in events)


def test_rejects_api_key_for_a_different_calendar(api):
    api.calendar["slug"] = "someone-else"
    with pytest.raises(ConnectorError) as excinfo:
        collect(api)
    assert excinfo.value.code == "calendar_mismatch"
    assert [request.url.path for request in api.requests] == ["/v1/calendars/get"]


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, text="not json", headers={"Content-Type": "application/json"}),
        httpx.Response(200, json=["not", "an", "object"]),
    ],
)
def test_rejects_unexpected_responses(api, response):
    api.override = response
    with pytest.raises(ConnectorError) as excinfo:
        collect(api)
    assert excinfo.value.code == "unexpected_response"


def test_stops_after_max_pages(api):
    endless = copy.deepcopy(api.pages[None])
    endless["next_cursor"] = "again"
    api.pages = {None: endless, "again": endless}

    collect(api)

    list_requests = [r for r in api.requests if r.url.path.endswith("/events/list")]
    assert len(list_requests) == luma.MAX_PAGES


@pytest.mark.django_db
class TestLumaIngestion:
    @pytest.fixture
    def source(self):
        return Source.objects.create(
            url=SOURCE_URL, connector="luma", status=Source.Status.APPROVED
        )

    def ingest(self, source, api, environ=None):
        return ingest_source(
            source.pk,
            fetcher_factory=api.fetcher_factory,
            environ={"LUMA_API_KEY": API_KEY} if environ is None else environ,
        )

    def test_ingests_and_is_idempotent(self, source, api):
        first = self.ingest(source, api)
        assert first.status == SourceRun.Status.SUCCEEDED, first.error_message
        assert first.events_created == 4
        breakfast = Event.objects.get(external_id="evt-synthetic0002")
        assert breakfast.venue_name == ""
        assert breakfast.address == "Accra, Greater Accra"

        second = self.ingest(source, api)
        assert (second.events_created, second.events_updated, second.events_unchanged) == (0, 0, 4)

    def test_missing_credentials_fail_without_network_calls(self, source, api):
        run = self.ingest(source, api, environ={})
        assert run.status == SourceRun.Status.FAILED
        assert run.error_code == "missing_credentials"
        assert api.requests == []

    @pytest.mark.parametrize(("status", "code"), [(401, "http_status"), (429, "http_status")])
    def test_api_errors_fail_the_run_without_leaking_the_key(
        self, source, api, status, code, caplog
    ):
        api.override = httpx.Response(status)
        with caplog.at_level(logging.DEBUG):
            run = self.ingest(source, api)
        assert run.status == SourceRun.Status.FAILED
        assert run.error_code == code
        assert API_KEY not in run.error_message
        assert API_KEY not in caplog.text
        assert not Event.objects.exists()


@pytest.mark.django_db
def test_register_luma_source():
    source = register_source(
        "https://lu.ma/Example-Tech",
        config={"api_key_env": "LUMA_API_KEY_ACCRA"},
        resolver=public_resolver,
        environ={"LUMA_API_KEY_ACCRA": API_KEY},
    )
    assert source.url == SOURCE_URL
    assert source.connector == "luma"
    assert source.config == {"api_key_env": "LUMA_API_KEY_ACCRA"}
    assert API_KEY not in json.dumps(source.config)

    with pytest.raises(SourceRegistrationError) as excinfo:
        register_source("https://luma.com/other-cal", resolver=public_resolver, environ={})
    assert excinfo.value.code == "missing_credentials"

    with pytest.raises(SourceRegistrationError) as excinfo:
        register_source(
            "https://luma.com/third-cal",
            config={"api_key": API_KEY},
            resolver=public_resolver,
            environ={"LUMA_API_KEY": API_KEY},
        )
    assert excinfo.value.code == "invalid_config"
