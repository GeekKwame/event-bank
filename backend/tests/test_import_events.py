import json
from datetime import UTC, datetime

import pytest
from django.core.management import CommandError, call_command

from apps.events.models import Event, EventRevision
from apps.ingestion.imports import ImportFileError, parse_event_file
from apps.ingestion.models import SourceRun
from apps.ingestion.services import IngestionError, ingest_source
from apps.sources.models import Source

SOURCE_URL = "https://community.example.org/"

EVENTS = [
    {
        "id": "meetup-2026-11",
        "title": "Example Developer Meetup",
        "url": "https://events.example.org/e/meetup",
        "starts_at": "2026-11-14T16:00:00",
        "ends_at": "2026-11-14T19:00:00",
        "venue_name": "Example Hub",
        "address": "1 Example Street, Accra",
        "attendance_mode": "offline",
        "organizer_name": "Example Community",
    },
    {
        "id": "webinar-2026-12",
        "title": "Example Webinar",
        "url": "https://events.example.org/e/webinar",
        "starts_at": "2026-12-02T18:00:00+01:00",
        "attendance_mode": "online",
    },
]


@pytest.fixture
def events_file(tmp_path):
    def write(rows):
        path = tmp_path / "events.json"
        path.write_text(json.dumps(rows))
        return path

    return write


def run_import(path, **kwargs):
    call_command("import_events", str(path), "--source-url", SOURCE_URL, **kwargs)


@pytest.mark.parametrize(
    ("rows", "message"),
    [
        ({"not": "a list"}, "non-empty JSON array"),
        ([], "non-empty JSON array"),
        (["text"], "must be a JSON object"),
        ([{**EVENTS[0], "colour": "blue"}], "unknown fields"),
        ([{k: v for k, v in EVENTS[0].items() if k != "url"}], "missing required fields"),
        ([{**EVENTS[0], "starts_at": "next tuesday"}], "not a valid ISO 8601"),
        ([{**EVENTS[0], "attendance_mode": "hybrid"}], "attendance_mode"),
        ([{**EVENTS[0], "status": "maybe"}], "status"),
        ([{**EVENTS[0], "title": 42}], "title must be a string"),
    ],
)
def test_parse_rejects_invalid_files(rows, message):
    with pytest.raises(ImportFileError, match=message):
        parse_event_file(json.dumps(rows).encode())


def test_parse_rejects_non_json():
    with pytest.raises(ImportFileError, match="not valid JSON"):
        parse_event_file(b"\xff\xfe not json")


@pytest.mark.django_db
def test_import_creates_approved_manual_source_and_events(events_file):
    run_import(events_file(EVENTS), source_name="Example Community")

    source = Source.objects.get()
    assert (source.url, source.connector, source.status) == (SOURCE_URL, "manual", "approved")
    assert source.name == "Example Community"
    meetup = Event.objects.get(external_id="meetup-2026-11")
    assert meetup.starts_at == datetime(2026, 11, 14, 16, tzinfo=UTC)
    assert meetup.timezone == "Africa/Accra"
    webinar = Event.objects.get(external_id="webinar-2026-12")
    assert webinar.starts_at == datetime(2026, 12, 2, 17, tzinfo=UTC)
    assert SourceRun.objects.get().trigger == SourceRun.Trigger.IMPORT


@pytest.mark.django_db
def test_reimport_is_idempotent_and_tracks_changes(events_file):
    run_import(events_file(EVENTS))
    run_import(events_file(EVENTS))
    assert Event.objects.count() == 2
    assert not EventRevision.objects.filter(kind="updated").exists()

    changed = [{**EVENTS[0], "starts_at": "2026-11-14T17:00:00"}]
    run_import(events_file(changed))

    revision = EventRevision.objects.get(kind="updated")
    assert revision.changed_fields == ["starts_at"]
    assert revision.is_significant
    assert Event.objects.filter(external_id="webinar-2026-12").exists()


@pytest.mark.django_db
def test_invalid_file_writes_nothing(events_file):
    with pytest.raises(CommandError, match="invalid_file"):
        run_import(events_file([{**EVENTS[0], "colour": "blue"}]))
    assert not Event.objects.exists()


@pytest.mark.django_db
def test_unsafe_link_is_skipped_and_reported(events_file, capsys):
    rows = [EVENTS[0], {**EVENTS[1], "url": "javascript:alert(1)"}]
    run_import(events_file(rows))
    assert list(Event.objects.values_list("external_id", flat=True)) == ["meetup-2026-11"]
    assert "invalid_url" in capsys.readouterr().err


@pytest.mark.django_db
def test_rejects_source_urls_used_by_fetched_sources(events_file):
    Source.objects.create(url=SOURCE_URL, connector="luma", status=Source.Status.APPROVED)
    with pytest.raises(CommandError, match="already uses this URL"):
        run_import(events_file(EVENTS))
    with pytest.raises(CommandError, match="unsupported_scheme"):
        call_command(
            "import_events", str(events_file(EVENTS)), "--source-url", "http://insecure.example"
        )


@pytest.mark.django_db
def test_manual_sources_cannot_be_fetched(events_file):
    run_import(events_file(EVENTS))
    with pytest.raises(IngestionError) as excinfo:
        ingest_source(Source.objects.get().pk)
    assert excinfo.value.code == "manual_source"
