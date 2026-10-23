from datetime import timedelta

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from apps.events.models import Event
from apps.ingestion.services import ingest_source
from apps.sources.models import Source
from tests.conftest import EXAMPLE_SOURCE_URL, jsonld_page

pytestmark = pytest.mark.django_db


@pytest.fixture
def populated_source(example_connector, fake_site, base_events):
    for event in base_events:
        event["startDate"] = (timezone.now() + timedelta(days=2)).isoformat()
        event["endDate"] = (timezone.now() + timedelta(days=2, hours=3)).isoformat()
    fake_site.html = jsonld_page(base_events)
    source = Source.objects.create(
        url=EXAMPLE_SOURCE_URL,
        connector="example",
        status=Source.Status.APPROVED,
        config={"api_key_env": "PRIVATE_CONFIG_REFERENCE"},
    )
    # The synthetic connector accepts no config; set private config after ingestion.
    source.config = {}
    source.save()
    ingest_source(source.pk, fetcher_factory=fake_site.fetcher_factory)
    source.config = {"api_key_env": "PRIVATE_CONFIG_REFERENCE"}
    source.save()
    return source


def test_events_public_paginated_filterable_and_attributed(populated_source):
    client = APIClient()
    response = client.get("/api/events/", secure=True)
    assert response.status_code == 200 and response.data["count"] == 2
    item = response.data["results"][0]
    assert {
        "title",
        "starts_at",
        "address",
        "url",
        "source_url",
        "last_seen_at",
        "description",
    } <= item.keys()
    assert client.get("/api/events/?location=Accra", secure=True).data["count"] == 1
    assert client.get("/api/events/?attendance_mode=online", secure=True).data["count"] == 1
    assert client.get("/api/events/?attendance_mode=invalid", secure=True).status_code == 400
    assert "PRIVATE_CONFIG_REFERENCE" not in str(response.data)
    assert "PRIVATE_CONFIG_REFERENCE" not in str(client.get("/api/sources/", secure=True).data)


def test_paused_and_past_events_hidden(populated_source):
    client = APIClient()
    Event.objects.filter(external_id="evt-meetup").update(
        starts_at=timezone.now() - timedelta(days=2), ends_at=timezone.now() - timedelta(days=1)
    )
    assert client.get("/api/events/", secure=True).data["count"] == 1
    populated_source.review(Source.Status.PAUSED)
    assert client.get("/api/events/", secure=True).data["count"] == 0
    assert client.get("/api/sources/", secure=True).data["count"] == 0


def test_multiday_date_only_event_visible_on_final_date(populated_source):
    today = timezone.now().replace(hour=0, minute=0, second=0, microsecond=0)
    Event.objects.filter(external_id="evt-meetup").update(
        is_all_day=True, timezone="UTC", starts_at=today - timedelta(days=2), ends_at=today
    )
    assert APIClient().get("/api/events/", secure=True).data["count"] == 2


def test_source_submission_requires_login_and_review(
    example_connector, django_user_model, monkeypatch
):
    monkeypatch.setattr("apps.sources.services.validate_fetch_target", lambda *a, **kw: None)
    client = APIClient()
    url = "/api/sources/submit/"
    assert client.post(url, {"url": EXAMPLE_SOURCE_URL}, secure=True).status_code == 403
    user = django_user_model.objects.create_user(username="synthetic-user", password="test-pass")
    client.force_authenticate(user)
    response = client.post(url, {"url": EXAMPLE_SOURCE_URL, "approve": True}, secure=True)
    assert response.status_code == 201
    source = Source.objects.get(pk=response.data["id"])
    assert source.status == "pending" and source.submitted_by == user
    assert client.post(url, {"url": EXAMPLE_SOURCE_URL}, secure=True).status_code == 400
    bad = client.post(url, {"url": "https://unknown.example.org/events"}, secure=True)
    assert bad.status_code == 400 and str(bad.data["code"]) == "unsupported_source"


def test_platform_directory_is_public(example_connector):
    response = APIClient().get("/api/platforms/", secure=True)
    assert response.status_code == 200
    assert response.data == [
        {"slug": "example", "name": "Example (tests only)", "hosts": ["events.example.org"]}
    ]
