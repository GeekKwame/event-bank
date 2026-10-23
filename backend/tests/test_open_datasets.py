"""Synthetic event records model published JSON schemas, not real attendees/events."""

import copy
import json
from datetime import UTC, date, datetime

import httpx
import pytest

from apps.events.models import Event, EventRevision
from apps.ingestion.services import ingest_source
from apps.sources.services import fetch_policy_for, register_source
from connectors.base import ConnectorError, SourceSpec
from connectors.http import SafeFetcher
from connectors.platforms.agentconfs import FEED_URL, AgentConfsConnector
from connectors.platforms.confstech import DATA_LICENSE, ConfsTechConnector
from connectors.registry import get_connector, match_connector
from pipeline.normalize import normalize_events
from tests.conftest import PUBLIC_IP, public_resolver

CONFS_URL = (
    "https://raw.githubusercontent.com/tech-conferences/conference-data/"
    "main/conferences/2026/python.json"
)
AGENT_ROW = {
    "id": "synthetic-1",
    "name": "Example AI Workshop",
    "official_url": "https://events.example.org/ai",
    "page": "https://agentconfs.com/events/example-ai-workshop",
    "start_date": "2026-11-14",
    "end_date": "2026-11-15",
    "format": "in-person",
    "venue": "Example Hall",
    "city": "Accra",
    "country": "Ghana",
    "organizer": "Example Community",
    "dates_to_be_confirmed": False,
    "attendees": [{"email": "private@example.org"}],
}
CONFS_ROW = {
    "name": "Example Python Conference",
    "url": "https://events.example.org/python",
    "startDate": "2026-11-14",
    "endDate": "2026-11-15",
    "city": "Accra",
    "country": "Ghana",
    "online": False,
}
DATASETS = [
    (AgentConfsConnector, FEED_URL, AGENT_ROW),
    (ConfsTechConnector, CONFS_URL, CONFS_ROW),
]


class DatasetSite:
    def __init__(self, connector, payload):
        self.connector = connector
        self.payload = payload
        self.status = 200
        self.requests = []

    def handler(self, request):
        self.requests.append(request)
        mime = "text/plain" if self.connector is ConfsTechConnector else "application/json"
        return httpx.Response(
            self.status, text=json.dumps(self.payload), headers={"Content-Type": mime}
        )

    def fetcher_factory(self, policy):
        return SafeFetcher(
            policy, resolver=public_resolver, transport=httpx.MockTransport(self.handler)
        )


def collect(connector, url, payload, config=None):
    site = DatasetSite(connector, payload)
    with site.fetcher_factory(fetch_policy_for(connector)) as fetcher:
        events = list(connector().collect(SourceSpec(url, "UTC", config or {}), fetcher))
    return events, site


@pytest.mark.parametrize(("connector", "url", "row"), DATASETS)
def test_dataset_mapping_and_fetch_policy(connector, url, row):
    events, site = collect(connector, url, [row])
    result = normalize_events(events, default_timezone="UTC")
    assert not result.skipped
    event = result.events[0]
    assert event.title == row["name"]
    assert event.starts_at == datetime(2026, 11, 14, tzinfo=UTC)
    assert event.ends_at == datetime(2026, 11, 15, tzinfo=UTC)
    assert event.is_all_day
    assert event.attendance_mode == "offline"
    assert event.address == "Accra, Ghana"
    assert "private@example.org" not in repr(events)
    assert "Event Bank" in event.description
    assert site.requests[0].url.host == PUBLIC_IP
    assert "authorization" not in site.requests[0].headers
    assert "x-luma-api-key" not in site.requests[0].headers
    assert get_connector(connector.slug) is connector
    assert match_connector(url) is connector


@pytest.mark.parametrize(("connector", "url", "row"), DATASETS)
def test_optional_country_filter(connector, url, row):
    assert len(collect(connector, url, [row], {"country": " ghana "})[0]) == 1
    assert collect(connector, url, [row], {"country": "Kenya"})[0] == []


@pytest.mark.parametrize("connector", [AgentConfsConnector, ConfsTechConnector])
@pytest.mark.parametrize("config", [{"api_key": "secret"}, {"country": ""}, {"country": 123}])
def test_rejects_invalid_config(connector, config):
    with pytest.raises(ConnectorError) as exc:
        connector.validate_config(config)
    assert exc.value.code == "invalid_config"


@pytest.mark.parametrize(
    ("connector", "url"),
    [
        (AgentConfsConnector, "https://agentconfs.com/events/example"),
        (AgentConfsConnector, "http://agentconfs.com/"),
        (AgentConfsConnector, "https://agentconfs.com.evil.example/"),
        (ConfsTechConnector, "https://raw.githubusercontent.com/another/repo/main/events.json"),
        (ConfsTechConnector, CONFS_URL.replace("2026", "latest")),
        (ConfsTechConnector, CONFS_URL.replace("main/", "untrusted-branch/")),
        (ConfsTechConnector, CONFS_URL.replace("python.json", "../private.json")),
    ],
)
def test_rejects_unrelated_source_urls(connector, url):
    assert not connector.matches(url)


def test_source_aliases_canonicalize_without_tracking_queries():
    assert AgentConfsConnector.canonicalize_url("https://agentconfs.com/?utm_source=x") == FEED_URL
    alias = CONFS_URL.replace("/main/", "/refs/heads/main/") + "?x=1#top"
    assert ConfsTechConnector.matches(alias)
    assert ConfsTechConnector.canonicalize_url(alias) == CONFS_URL


@pytest.mark.parametrize(("connector", "url", "row"), DATASETS)
@pytest.mark.parametrize("payload", [{}, {"events": {}}, [None], "not a dataset"])
def test_wrong_shapes_fail_explicitly(connector, url, row, payload):
    with pytest.raises(ConnectorError) as exc:
        collect(connector, url, payload)
    assert exc.value.code == "unexpected_response"


@pytest.mark.parametrize(("connector", "url", "row"), DATASETS)
def test_bad_dates_fail_explicitly(connector, url, row):
    row = {**row, ("start_date" if connector is AgentConfsConnector else "startDate"): "wrong"}
    with pytest.raises(ConnectorError) as exc:
        collect(connector, url, [row])
    assert exc.value.code == "unexpected_response"


def test_agentconfs_envelope_tentative_dates_and_attribution():
    row = {**AGENT_ROW, "dates_to_be_confirmed": True, "format": "hybrid"}
    events, _ = collect(AgentConfsConnector, FEED_URL, {"events": [row]})
    event = events[0]
    assert event.starts_at is None and event.ends_at is None
    assert event.attendance_mode == "mixed"
    assert "Dates to be confirmed" in event.description
    assert "CC BY 4.0" in event.description
    assert row["page"] in event.description
    assert event.organizer_name == "Example Community"


def test_agentconfs_id_survives_reschedule_and_url_changes():
    before, _ = collect(AgentConfsConnector, FEED_URL, [AGENT_ROW])
    row = {**AGENT_ROW, "start_date": "2026-12-14", "official_url": "https://example.org/new"}
    after, _ = collect(AgentConfsConnector, FEED_URL, [row])
    assert before[0].external_id == after[0].external_id


@pytest.mark.parametrize(("connector", "url", "row"), DATASETS)
def test_missing_optional_fields_and_invalid_identity(connector, url, row):
    keys = (
        ("id", "name", "official_url")
        if connector is AgentConfsConnector
        else ("name", "url", "startDate")
    )
    minimal = {key: row[key] for key in keys}
    events, _ = collect(connector, url, [minimal])
    assert len(normalize_events(events, default_timezone="UTC").events) == 1
    minimal.pop("id" if connector is AgentConfsConnector else "startDate")
    events, _ = collect(connector, url, [minimal])
    assert (
        normalize_events(events, default_timezone="UTC").skipped[0].reason == "missing_external_id"
    )


def test_confstech_recurring_urls_do_not_collapse_and_ids_ignore_title_edits():
    other = {**CONFS_ROW, "startDate": "2026-12-01", "endDate": "2026-12-01"}
    events, _ = collect(ConfsTechConnector, CONFS_URL, [CONFS_ROW, other])
    assert events[0].external_id != events[1].external_id
    renamed, _ = collect(ConfsTechConnector, CONFS_URL, [{**CONFS_ROW, "name": "Renamed"}])
    assert renamed[0].external_id == events[0].external_id
    assert DATA_LICENSE in events[0].description
    assert events[1].starts_at == date(2026, 12, 1)


@pytest.mark.django_db
@pytest.mark.parametrize(("connector", "url", "row"), DATASETS)
def test_register_ingest_repeat_change_and_failure(connector, url, row):
    source = register_source(
        url, timezone="UTC", approve=True, resolver=public_resolver, environ={}
    )
    site = DatasetSite(connector, [copy.deepcopy(row)])

    first = ingest_source(source.pk, fetcher_factory=site.fetcher_factory, environ={})
    assert first.status == "succeeded", first.error_message
    assert first.events_created == 1
    event = Event.objects.get(source=source)
    assert ("CC BY 4.0" if connector is AgentConfsConnector else "MIT license") in event.description

    second = ingest_source(source.pk, fetcher_factory=site.fetcher_factory, environ={})
    assert second.events_unchanged == 1
    assert EventRevision.objects.filter(event=event).count() == 1

    site.payload[0]["name"] = "Renamed workshop"
    third = ingest_source(source.pk, fetcher_factory=site.fetcher_factory, environ={})
    assert third.events_updated == 1
    revision = EventRevision.objects.get(event=event, kind="updated")
    assert revision.changed_fields == ["title"]

    site.status = 403
    failed = ingest_source(source.pk, fetcher_factory=site.fetcher_factory, environ={})
    assert failed.status == "failed" and failed.error_code == "http_status"
    event.refresh_from_db()
    assert event.title == "Renamed workshop"
    assert EventRevision.objects.filter(event=event).count() == 2
