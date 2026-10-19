from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path

from connectors.base import AttendanceMode, EventStatus
from connectors.extract.jsonld import find_events, parse_datetime, to_raw_event

FIXTURE = (Path(__file__).parent / "fixtures" / "schema_org_events.html").read_text()
BASE_URL = "https://events.example.org/calendar"


def test_finds_events_across_blocks_and_skips_invalid_json_and_non_events():
    events = find_events(FIXTURE)
    assert [event["name"] for event in events] == [
        "  Product   Launch &amp; Mixer ",
        "Example Hackathon",
    ]


def test_maps_schema_org_fields_to_raw_event():
    node = find_events(FIXTURE)[0]
    raw = to_raw_event(node, external_id="launch", base_url=BASE_URL)

    assert raw.external_id == "launch"
    assert raw.url == "https://events.example.org/e/launch"
    assert raw.starts_at == datetime(2026, 11, 20, 18, 0)
    assert raw.status is EventStatus.CANCELLED
    assert raw.attendance_mode is AttendanceMode.MIXED
    assert raw.venue_name == "Example Rooftop"
    assert raw.address == "5 Sample Road, Accra"
    assert raw.organizer_name == "Example Startup"
    assert raw.image_url == "https://events.example.org/img/launch.png"


def test_prefixed_types_date_only_values_and_relative_urls():
    node = find_events(FIXTURE)[1]
    raw = to_raw_event(node, external_id="hack-1", base_url=BASE_URL)
    assert raw.starts_at == date(2026, 12, 5)
    assert raw.url == BASE_URL
    assert raw.attendance_mode is AttendanceMode.UNKNOWN
    assert raw.status is EventStatus.SCHEDULED

    relative = to_raw_event({"name": "x", "url": "/e/x"}, external_id="x", base_url=BASE_URL)
    assert relative.url == "https://events.example.org/e/x"


def test_parse_datetime_handles_offsets_utc_suffix_and_garbage():
    assert parse_datetime("2026-11-20T18:00:00+01:00") == datetime(
        2026, 11, 20, 18, tzinfo=timezone(timedelta(hours=1))
    )
    assert parse_datetime("2026-11-20T18:00:00.000Z") == datetime(2026, 11, 20, 18, tzinfo=UTC)
    assert parse_datetime("next tuesday") is None
    assert parse_datetime(None) is None
