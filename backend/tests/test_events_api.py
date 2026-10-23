from datetime import timedelta
from itertools import count

import pytest
from django.utils import timezone

from apps.events.models import Event
from apps.ingestion.models import SourceRun
from apps.sources.models import Source

pytestmark = pytest.mark.django_db

_ids = count(1)


def make_source(status=Source.Status.APPROVED, **kwargs):
    n = next(_ids)
    return Source.objects.create(
        url=kwargs.pop("url", f"https://organiser{n}.example.org/"),
        connector=kwargs.pop("connector", "manual"),
        name=kwargs.pop("name", f"Organiser {n}"),
        status=status,
        **kwargs,
    )


def make_event(source, *, starts_in=timedelta(days=3), duration=timedelta(hours=2), **kwargs):
    now = timezone.now()
    starts_at = now + starts_in if starts_in is not None else None
    values = {
        "external_id": f"evt-{next(_ids)}",
        "title": "Example Event",
        "url": "https://events.example.org/e/1",
        "starts_at": starts_at,
        "ends_at": starts_at + duration if starts_at and duration else None,
        "timezone": "Africa/Accra",
        "content_hash": "0" * 64,
        "first_seen_at": now,
        "last_seen_at": now,
        "last_changed_at": now,
    }
    values.update(kwargs)
    return Event.objects.create(source=source, **values)


def get(client, path="/api/events/", **params):
    return client.get(path, params, secure=True)


def titles(response):
    return [event["title"] for event in response.json()["results"]]


def test_is_public_and_lists_upcoming_events_soonest_first(client):
    source = make_source()
    make_event(source, title="Later", starts_in=timedelta(days=10))
    make_event(source, title="Sooner", starts_in=timedelta(days=1))
    make_event(source, title="Ongoing", starts_in=-timedelta(hours=1), duration=timedelta(hours=3))
    make_event(source, title="Finished", starts_in=-timedelta(days=2))
    make_event(source, title="No end, started", starts_in=-timedelta(hours=1), duration=None)
    make_event(source, title="Undated", starts_in=None)

    response = get(client)

    assert response.status_code == 200
    assert titles(response) == ["Ongoing", "Sooner", "Later"]
    assert response.json()["count"] == 3


def test_past_and_all(client):
    source = make_source()
    make_event(source, title="Upcoming", starts_in=timedelta(days=1))
    make_event(source, title="Old", starts_in=-timedelta(days=10))
    make_event(source, title="Older", starts_in=-timedelta(days=20))

    assert titles(get(client, when="past")) == ["Old", "Older"]
    assert titles(get(client, when="all")) == ["Older", "Old", "Upcoming"]


def test_only_approved_sources_are_published(client):
    make_event(make_source(), title="Approved")
    for status in (Source.Status.PENDING, Source.Status.REJECTED, Source.Status.PAUSED):
        hidden = make_event(make_source(status=status), title=f"Hidden {status}")
        assert get(client, f"/api/events/{hidden.pk}/").status_code == 404

    assert titles(get(client)) == ["Approved"]


def test_cancelled_events_are_hidden_unless_requested(client):
    source = make_source()
    make_event(source, title="On", starts_in=timedelta(days=1))
    make_event(source, title="Off", starts_in=timedelta(days=2), status=Event.Status.CANCELLED)

    assert titles(get(client)) == ["On"]
    assert titles(get(client, include_cancelled="true")) == ["On", "Off"]


def test_location_and_attendance_filters(client):
    source = make_source()
    make_event(
        source,
        title="Accra meetup",
        starts_in=timedelta(days=1),
        venue_name="Example Hub",
        address="1 Example Street, Accra",
        attendance_mode=Event.AttendanceMode.OFFLINE,
    )
    make_event(
        source,
        title="Kumasi hybrid",
        starts_in=timedelta(days=2),
        address="Kumasi",
        attendance_mode=Event.AttendanceMode.MIXED,
    )
    make_event(
        source,
        title="Webinar",
        starts_in=timedelta(days=3),
        attendance_mode=Event.AttendanceMode.ONLINE,
    )

    assert titles(get(client, location="accra")) == ["Accra meetup"]
    assert titles(get(client, location="Example Hub")) == ["Accra meetup"]
    assert titles(get(client, attendance="online")) == ["Kumasi hybrid", "Webinar"]
    assert titles(get(client, attendance="in_person")) == ["Accra meetup", "Kumasi hybrid"]


def test_date_and_source_filters(client):
    first, second = make_source(), make_source()
    make_event(first, title="Week 1", starts_in=timedelta(days=2))
    make_event(second, title="Week 3", starts_in=timedelta(days=20))
    week_two = (timezone.localdate() + timedelta(days=10)).isoformat()

    assert titles(get(client, starts_after=week_two)) == ["Week 3"]
    assert titles(get(client, starts_before=week_two)) == ["Week 1"]
    assert titles(get(client, source=second.pk)) == ["Week 3"]


@pytest.mark.parametrize(
    "params",
    [
        {"when": "soon"},
        {"attendance": "hybrid"},
        {"starts_after": "next week"},
        {"source": "abc"},
        {"include_cancelled": "maybe"},
        {"location": "x" * 101},
    ],
)
def test_invalid_filters_return_400(client, params):
    assert get(client, **params).status_code == 400


def test_pagination(client):
    source = make_source()
    for day in range(1, 31):
        make_event(source, title=f"Day {day:02d}", starts_in=timedelta(days=day))

    first = get(client).json()
    assert (first["count"], len(first["results"])) == (30, 25)
    assert first["next"] is not None
    assert len(get(client, page=2).json()["results"]) == 5
    assert len(get(client, page_size=500).json()["results"]) == 30
    assert get(client, page=9).status_code == 404


def test_fields_attribution_and_freshness(client):
    source = make_source(name="Example Community", url="https://community.example.org/")
    event = make_event(
        source,
        title="Example Developer Meetup",
        url="https://events.example.org/e/meetup",
        venue_name="Example Hub",
        address="Accra",
        attendance_mode=Event.AttendanceMode.OFFLINE,
    )
    now = timezone.now()
    SourceRun.objects.create(
        source=source, status=SourceRun.Status.SUCCEEDED, finished_at=now - timedelta(hours=2)
    )
    SourceRun.objects.create(
        source=source, status=SourceRun.Status.SUCCEEDED, finished_at=now - timedelta(hours=1)
    )
    SourceRun.objects.create(source=source, status=SourceRun.Status.FAILED, finished_at=now)

    data = get(client, f"/api/events/{event.pk}/").json()

    assert data["title"] == "Example Developer Meetup"
    assert data["url"] == "https://events.example.org/e/meetup"
    assert data["attendance_mode"] == "offline"
    assert data["last_seen_at"] is not None
    assert data["source"]["name"] == "Example Community"
    assert data["source"]["platform"] == "Verified submission"
    assert data["source"]["url"] == "https://community.example.org/"
    checked = timezone.datetime.fromisoformat(data["source"]["last_checked_at"])
    assert abs(checked - (now - timedelta(hours=1))) < timedelta(seconds=1)
    assert get(client).json()["results"][0] == data


def test_source_never_checked_has_null_last_checked_at(client):
    make_event(make_source())
    assert get(client).json()["results"][0]["source"]["last_checked_at"] is None


def test_query_count_does_not_grow_with_results(client, django_assert_max_num_queries):
    source = make_source()
    for day in range(1, 21):
        make_event(source, starts_in=timedelta(days=day))
    with django_assert_max_num_queries(3):
        assert len(get(client).json()["results"]) == 20


def test_read_only(client):
    event = make_event(make_source())
    for method in ("post", "put", "patch", "delete"):
        response = getattr(client, method)(f"/api/events/{event.pk}/", secure=True)
        assert response.status_code == 405
    assert client.post("/api/events/", {}, secure=True).status_code == 405
