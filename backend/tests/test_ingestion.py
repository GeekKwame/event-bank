from datetime import UTC, datetime

import httpx
import pytest

from apps.events import services as event_services
from apps.events.models import Event, EventRevision
from apps.ingestion.models import SourceRun
from apps.ingestion.services import SourceNotApprovedError, ingest_source
from apps.sources.models import Source
from tests.conftest import EXAMPLE_SOURCE_URL, PUBLIC_IP, jsonld_page

pytestmark = pytest.mark.django_db


@pytest.fixture
def source(example_connector):
    return Source.objects.create(
        url=EXAMPLE_SOURCE_URL, connector="example", status=Source.Status.APPROVED
    )


def run(source, fake_site):
    return ingest_source(source.pk, fetcher_factory=fake_site.fetcher_factory)


def event_state():
    return list(Event.objects.order_by("external_id").values())


def test_first_run_creates_events_and_revisions(source, fake_site):
    result = run(source, fake_site)

    assert result.status == SourceRun.Status.SUCCEEDED, result.error_message
    assert (result.events_found, result.events_created) == (2, 2)
    meetup = Event.objects.get(external_id="evt-meetup")
    assert meetup.url == "https://events.example.org/e/meetup"
    assert meetup.starts_at == datetime(2026, 11, 14, 9, tzinfo=UTC)
    assert meetup.venue_name == "Example Hall"
    workshop = Event.objects.get(external_id="evt-workshop")
    assert workshop.starts_at == datetime(2026, 12, 1, 17, 30, tzinfo=UTC)
    assert workshop.attendance_mode == "online"
    assert workshop.url == "https://events.example.org/e/workshop"
    assert EventRevision.objects.filter(kind="created", is_significant=True).count() == 2
    assert fake_site.requests[0].url.host == PUBLIC_IP


def test_repeated_ingestion_is_idempotent(source, fake_site):
    run(source, fake_site)
    before = event_state()

    second = run(source, fake_site)

    assert second.status == SourceRun.Status.SUCCEEDED
    assert (second.events_created, second.events_updated, second.events_unchanged) == (0, 0, 2)
    assert Event.objects.count() == 2
    assert EventRevision.objects.count() == 2
    after = event_state()
    for old, new in zip(before, after, strict=True):
        assert new["last_seen_at"] > old["last_seen_at"]
        assert new["last_changed_at"] == old["last_changed_at"]
        assert new["content_hash"] == old["content_hash"]


def test_start_time_change_creates_one_significant_revision(source, fake_site, base_events):
    run(source, fake_site)
    base_events[0]["startDate"] = "2026-11-14T10:30:00+00:00"
    fake_site.html = jsonld_page(base_events)

    result = run(source, fake_site)

    assert (result.events_updated, result.events_unchanged) == (1, 1)
    revision = EventRevision.objects.get(kind="updated")
    assert revision.event.external_id == "evt-meetup"
    assert revision.changed_fields == ["starts_at"]
    assert revision.is_significant
    assert revision.before == {"starts_at": "2026-11-14T09:00:00+00:00"}
    assert revision.after == {"starts_at": "2026-11-14T10:30:00+00:00"}
    assert revision.source_run == result
    assert Event.objects.get(external_id="evt-meetup").starts_at == datetime(
        2026, 11, 14, 10, 30, tzinfo=UTC
    )


def test_description_only_change_is_insignificant(source, fake_site, base_events):
    run(source, fake_site)
    base_events[0]["description"] = "A day of talks and demos."
    fake_site.html = jsonld_page(base_events)

    run(source, fake_site)

    revision = EventRevision.objects.get(kind="updated")
    assert revision.changed_fields == ["description"]
    assert not revision.is_significant


def test_cancellation_is_significant(source, fake_site, base_events):
    run(source, fake_site)
    base_events[1]["eventStatus"] = "https://schema.org/EventCancelled"
    fake_site.html = jsonld_page(base_events)

    run(source, fake_site)

    revision = EventRevision.objects.get(kind="updated")
    assert revision.changed_fields == ["status"]
    assert revision.is_significant
    assert Event.objects.get(external_id="evt-workshop").status == "cancelled"


def test_events_missing_from_a_fetch_are_not_deleted(source, fake_site, base_events):
    run(source, fake_site)
    workshop_before = Event.objects.get(external_id="evt-workshop")
    fake_site.html = jsonld_page(base_events[:1])

    result = run(source, fake_site)

    assert result.status == SourceRun.Status.SUCCEEDED
    workshop_after = Event.objects.get(external_id="evt-workshop")
    assert workshop_after.last_seen_at == workshop_before.last_seen_at
    assert workshop_after.content_hash == workshop_before.content_hash


@pytest.mark.parametrize(
    ("response", "code"),
    [
        (httpx.Response(500), "http_status"),
        (httpx.Response(200, html="x" * (3 * 1024 * 1024)), "response_too_large"),
        (
            httpx.Response(302, headers={"Location": "https://169.254.169.254/"}),
            "ip_literal_not_allowed",
        ),
        (httpx.Response(200, html="<html>no events here</html>"), "no_events"),
    ],
)
def test_failed_runs_are_recorded_and_leave_events_untouched(
    source, fake_site, base_events, response, code
):
    run(source, fake_site)
    before = event_state()
    fake_site.response = response

    result = run(source, fake_site)

    assert result.status == SourceRun.Status.FAILED
    assert result.error_code == code
    assert result.finished_at is not None
    assert event_state() == before
    assert EventRevision.objects.count() == 2


def test_timeouts_fail_the_run(source, fake_site):
    def timeout(request):
        raise httpx.ReadTimeout("slow")

    fake_site.response = timeout
    result = run(source, fake_site)

    assert result.status == SourceRun.Status.FAILED
    assert result.error_code == "timeout"
    assert not Event.objects.exists()


def test_failure_midway_through_upsert_rolls_back_everything(
    source, fake_site, base_events, monkeypatch
):
    run(source, fake_site)
    before = event_state()
    base_events[0]["name"] = "Renamed meetup"
    base_events[1]["name"] = "Renamed workshop"
    fake_site.html = jsonld_page(base_events)

    original_create = EventRevision.objects.create
    calls = []

    def fail_on_second_revision(**kwargs):
        calls.append(kwargs)
        if len(calls) == 2:
            raise RuntimeError("database exploded")
        return original_create(**kwargs)

    monkeypatch.setattr(event_services.EventRevision.objects, "create", fail_on_second_revision)
    result = run(source, fake_site)

    assert result.status == SourceRun.Status.FAILED
    assert result.error_code == "internal_error"
    assert result.error_message == "RuntimeError"
    assert (result.events_created, result.events_updated) == (0, 0)
    assert event_state() == before
    assert EventRevision.objects.count() == 2


def test_unapproved_sources_are_not_ingested(example_connector, fake_site):
    pending = Source.objects.create(url=EXAMPLE_SOURCE_URL, connector="example")

    with pytest.raises(SourceNotApprovedError):
        run(pending, fake_site)
    assert not SourceRun.objects.exists()
    assert fake_site.requests == []
