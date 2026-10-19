import dataclasses
from datetime import UTC, date, datetime

import pytest

from connectors.base import AttendanceMode, RawEvent
from pipeline.diff import diff_snapshots
from pipeline.fingerprint import fingerprint
from pipeline.normalize import (
    NormalizationError,
    canonicalize_url,
    normalize_event,
    normalize_events,
    to_snapshot,
)


def raw(**overrides):
    values = {
        "external_id": "evt-1",
        "title": "Example",
        "url": "https://events.example.org/e/1",
        "starts_at": datetime(2026, 11, 14, 9, 0),
    }
    return RawEvent(**{**values, **overrides})


def test_naive_times_use_event_then_source_timezone():
    lagos = normalize_event(raw(), default_timezone="Africa/Lagos")
    assert lagos.starts_at == datetime(2026, 11, 14, 8, 0, tzinfo=UTC)
    assert lagos.timezone == "Africa/Lagos"

    explicit = normalize_event(raw(timezone="Africa/Accra"), default_timezone="Africa/Lagos")
    assert explicit.starts_at == datetime(2026, 11, 14, 9, 0, tzinfo=UTC)

    fallback = normalize_event(raw(timezone="Not/AZone"), default_timezone="Africa/Accra")
    assert fallback.timezone == "Africa/Accra"


def test_dates_become_all_day_events_and_bad_ranges_are_dropped():
    event = normalize_event(
        raw(starts_at=date(2026, 12, 5), ends_at=datetime(2026, 12, 4, 10, 0)),
        default_timezone="Africa/Accra",
    )
    assert event.is_all_day
    assert event.starts_at == datetime(2026, 12, 5, tzinfo=UTC)
    assert event.ends_at is None

    implausible = normalize_event(raw(starts_at=datetime(1900, 1, 1)), default_timezone="UTC")
    assert implausible.starts_at is None


def test_text_is_cleaned_and_capped():
    event = normalize_event(
        raw(
            title="  Launch &amp;\n\tMixer\x00 ",
            description="One\r\n\r\n\r\n\r\nTwo  words",
            venue_name="x" * 1000,
        ),
        default_timezone="UTC",
    )
    assert event.title == "Launch & Mixer"
    assert event.description == "One\n\nTwo words"
    assert len(event.venue_name) == 300


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (
            "HTTPS://Events.Example.org:443/e/1?utm_source=x&b=2&a=1#frag",
            "https://events.example.org/e/1?a=1&b=2",
        ),
        ("https://events.example.org", "https://events.example.org/"),
        ("javascript:alert(1)", ""),
        ("data:text/html,hi", ""),
        ("/relative", ""),
        ("", ""),
    ],
)
def test_canonicalize_url(value, expected):
    assert canonicalize_url(value) == expected


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"external_id": " "}, "missing_external_id"),
        ({"external_id": "x" * 300}, "external_id_too_long"),
        ({"title": ""}, "missing_title"),
        ({"url": "javascript:alert(1)"}, "invalid_url"),
        ({"attendance_mode": "teleport"}, "invalid_status_or_attendance_mode"),
    ],
)
def test_invalid_events_raise(overrides, reason):
    with pytest.raises(NormalizationError, match=reason):
        normalize_event(raw(**overrides), default_timezone="UTC")


def test_normalize_events_skips_invalid_and_duplicate_ids():
    result = normalize_events(
        [raw(), raw(title="Duplicate"), raw(external_id="evt-2", title="")],
        default_timezone="UTC",
    )
    assert [event.title for event in result.events] == ["Example"]
    assert [(s.external_id, s.reason) for s in result.skipped] == [
        ("evt-1", "duplicate"),
        ("evt-2", "missing_title"),
    ]


def test_fingerprint_is_stable_and_sensitive_to_tracked_fields():
    event = normalize_event(raw(), default_timezone="UTC")
    snapshot = to_snapshot(event)
    assert fingerprint(snapshot) == fingerprint(dict(reversed(list(snapshot.items()))))
    changed = to_snapshot(dataclasses.replace(event, title="Other"))
    assert fingerprint(snapshot) != fingerprint(changed)


def test_diff_classifies_significant_and_insignificant_changes():
    event = normalize_event(raw(), default_timezone="UTC")
    before = to_snapshot(event)

    assert not diff_snapshots(before, before).has_changes

    moved = diff_snapshots(
        before,
        to_snapshot(dataclasses.replace(event, starts_at=datetime(2026, 11, 15, tzinfo=UTC))),
    )
    assert moved.changed_fields == ("starts_at",)
    assert moved.is_significant
    assert moved.before == {"starts_at": "2026-11-14T09:00:00+00:00"}

    reworded = diff_snapshots(
        before, to_snapshot(dataclasses.replace(event, description="New", title="New title"))
    )
    assert set(reworded.changed_fields) == {"title", "description"}
    assert not reworded.is_significant

    online = diff_snapshots(
        before, to_snapshot(dataclasses.replace(event, attendance_mode=AttendanceMode.ONLINE))
    )
    assert online.is_significant
