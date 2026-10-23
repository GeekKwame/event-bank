"""Regression tests for the project audit findings (H1, M1-M3, L1, L2, L5, L6)."""

import copy
from datetime import timedelta

import pytest
from django.core.management import call_command
from django.db import connection
from django.utils import timezone

from apps.events.models import Event
from apps.ingestion import worker
from apps.ingestion.management.commands import run_worker
from apps.ingestion.services import MAX_EVENTS_PER_RUN, ingest_source
from apps.notifications.models import Delivery
from apps.notifications.providers.base import DeliveryOutcome, SendResult
from apps.notifications.retention import purge_expired_data
from apps.notifications.services import MAX_ALERTS_PER_RUN, deliver_one
from apps.sources.models import Source
from apps.sources.services import SourceRegistrationError, register_source
from apps.subscriptions import telegram
from apps.subscriptions.models import SourceRequest, Subscriber, Subscription, TelegramCursor
from apps.subscriptions.telegram import ERROR_REPLY, process_update
from connectors.platforms.luma import LumaCalendarConnector
from tests.conftest import BASE_EVENTS, EXAMPLE_SOURCE_URL, jsonld_page, public_resolver

pytestmark = pytest.mark.django_db

MALFORMED_URLS = ["https://luma[com/x", "https://[::1/x", "https://a]b/"]


def update(update_id, text, chat_id=12345):
    return {
        "update_id": update_id,
        "message": {
            "chat": {"type": "private", "id": chat_id},
            "from": {"id": chat_id, "is_bot": False},
            "text": text,
        },
    }


def reply_for(update_id):
    return Delivery.objects.get(dedupe_key=f"telegram:bot:update:{update_id}").message


def future_events(count):
    events = []
    for index in range(count):
        event = copy.deepcopy(BASE_EVENTS[0])
        event["@id"] = f"evt-{index}"
        event["name"] = f"Event {index:03d}"
        start = timezone.now() + timedelta(days=3, minutes=index)
        event["startDate"] = start.isoformat()
        event["endDate"] = (start + timedelta(hours=1)).isoformat()
        events.append(event)
    return events


@pytest.fixture
def followed(example_connector, fake_site):
    source = Source.objects.create(
        url=EXAMPLE_SOURCE_URL, connector="example", status=Source.Status.APPROVED
    )
    subscriber = Subscriber.objects.create(
        telegram_chat_id="12345", active=True, consented_at=timezone.now()
    )
    Subscription.objects.create(subscriber=subscriber, source=source)
    return source, subscriber


class StubProvider:
    def __init__(self):
        self.calls = []

    def send(self, recipients, message):
        self.calls.append(message)
        return SendResult(provider="telegram", outcome=DeliveryOutcome.ACCEPTED)


# H1: malformed input must never stall the Telegram cursor or crash registration.


@pytest.mark.parametrize("url", MALFORMED_URLS)
def test_malformed_urls_are_rejected_cleanly(url):
    assert not LumaCalendarConnector.matches(url)
    with pytest.raises(SourceRegistrationError) as excinfo:
        register_source(url, resolver=public_resolver, environ={})
    assert excinfo.value.code in {"invalid_url", "unsupported_source"}


def test_malformed_add_command_advances_cursor():
    assert process_update("bot", update(1, "/add https://luma[com/x"))
    assert "Source not added" in reply_for(1)
    assert TelegramCursor.objects.get(bot_id="bot").next_update_id == 2
    assert process_update("bot", update(2, "/help"))
    assert TelegramCursor.objects.get(bot_id="bot").next_update_id == 3


def test_unexpected_command_error_still_replies_and_advances(monkeypatch):
    def explode(subscriber, text):
        raise RuntimeError("bug")

    monkeypatch.setattr(telegram, "_command", explode)
    assert process_update("bot", update(7, "/sources"))
    assert reply_for(7) == ERROR_REPLY
    assert TelegramCursor.objects.get(bot_id="bot").next_update_id == 8


# M2: the worker survives failures and keeps cycles short.


def test_worker_cycle_survives_unexpected_errors(monkeypatch, settings):
    settings.TELEGRAM_SENDING_ENABLED = False

    def explode():
        raise RuntimeError("database hiccup")

    monkeypatch.setattr(run_worker, "ingest_due_sources", explode)
    call_command("run_worker", "--once")


def test_scheduler_skips_failing_and_deleted_sources_and_respects_limit(settings):
    settings.INGESTION_SCHEDULING_ENABLED = True
    ids = [
        Source.objects.create(
            url=f"https://s{n}.example.org/", connector="example", status="approved"
        ).pk
        for n in range(4)
    ]
    attempted = []

    def ingest(source_id, **kwargs):
        attempted.append(source_id)
        if source_id == ids[0]:
            raise RuntimeError("connector bug")
        if source_id == ids[1]:
            raise Source.DoesNotExist

    assert worker.ingest_due_sources(ingest=ingest, limit=1) == 1
    assert attempted == ids[:3]


# L5: the worker notices a lost advisory lock and re-acquires it.


def test_worker_lock_is_rechecked():
    assert run_worker._try_lock()
    assert run_worker._holds_lock()
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_unlock(%s)", [run_worker.WORKER_LOCK_ID])
    assert not run_worker._holds_lock()
    assert run_worker._try_lock() and run_worker._holds_lock()
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_unlock(%s)", [run_worker.WORKER_LOCK_ID])


# M1: one run can't flood a subscriber; command replies jump the queue.


def test_large_runs_send_one_digest(followed, fake_site):
    source, subscriber = followed
    fake_site.html = jsonld_page(future_events(MAX_ALERTS_PER_RUN + 3))

    run = ingest_source(source.pk, fetcher_factory=fake_site.fetcher_factory)

    assert run.events_created == MAX_ALERTS_PER_RUN + 3
    digest = Delivery.objects.get(subscriber=subscriber)
    assert digest.kind == Delivery.Kind.DIGEST and digest.source == source
    assert f"{MAX_ALERTS_PER_RUN + 3} new or updated" in digest.message
    assert "…and 3 more." in digest.message


def test_small_runs_send_individual_alerts(followed, fake_site):
    source, _ = followed
    fake_site.html = jsonld_page(future_events(MAX_ALERTS_PER_RUN))
    ingest_source(source.pk, fetcher_factory=fake_site.fetcher_factory)
    assert Delivery.objects.filter(kind=Delivery.Kind.ALERT).count() == MAX_ALERTS_PER_RUN


def test_replies_are_delivered_before_queued_alerts(followed, fake_site):
    source, _ = followed
    fake_site.html = jsonld_page(future_events(3))
    ingest_source(source.pk, fetcher_factory=fake_site.fetcher_factory)
    process_update("bot", update(1, "/stop"))
    provider = StubProvider()

    first = deliver_one(provider)

    assert first.kind == Delivery.Kind.REPLY
    assert provider.calls == [reply_for(1)]
    assert not Delivery.objects.filter(kind=Delivery.Kind.ALERT, status="pending").exists()


def test_unfollow_cancels_pending_digest(followed, fake_site):
    source, _ = followed
    fake_site.html = jsonld_page(future_events(MAX_ALERTS_PER_RUN + 1))
    ingest_source(source.pk, fetcher_factory=fake_site.fetcher_factory)
    process_update("bot", update(1, f"/unfollow {source.pk}"))
    digest = Delivery.objects.get(kind=Delivery.Kind.DIGEST)
    assert digest.status == "cancelled" and digest.error_code == "unfollowed"


def test_digest_rechecks_eligibility(followed, fake_site):
    source, _ = followed
    fake_site.html = jsonld_page(future_events(MAX_ALERTS_PER_RUN + 1))
    ingest_source(source.pk, fetcher_factory=fake_site.fetcher_factory)
    source.review(Source.Status.PAUSED)
    provider = StubProvider()
    assert deliver_one(provider).status == "cancelled"
    assert not provider.calls


# M3: too many events are truncated, not failed.


def test_oversized_runs_are_truncated_not_failed(followed, fake_site, settings):
    source, _ = followed
    Subscription.objects.all().delete()
    fake_site.html = jsonld_page(future_events(MAX_EVENTS_PER_RUN + 5))

    run = ingest_source(source.pk, fetcher_factory=fake_site.fetcher_factory)

    assert run.status == "succeeded", run.error_message
    assert run.events_created == MAX_EVENTS_PER_RUN
    assert run.skipped_details[0]["reason"].startswith("run_limit_reached")
    assert Event.objects.count() == MAX_EVENTS_PER_RUN


# L1/L2: duplicate replies don't leak review state; failed attempts are rate limited.


def test_duplicate_reply_only_reveals_approved_sources(example_connector, monkeypatch):
    monkeypatch.setattr("apps.sources.services.validate_fetch_target", lambda *a, **kw: None)
    pending = Source.objects.create(url=EXAMPLE_SOURCE_URL, connector="example")
    process_update("bot", update(1, f"/add {EXAMPLE_SOURCE_URL}"))
    assert reply_for(1) == "This source has already been submitted."
    assert str(pending.pk) not in reply_for(1)

    pending.review(Source.Status.APPROVED)
    process_update("bot", update(2, f"/add {EXAMPLE_SOURCE_URL}"))
    assert f"/follow {pending.pk}" in reply_for(2)


def test_failed_add_attempts_count_towards_the_limit(monkeypatch):
    calls = []
    real_register = telegram.register_source

    def counting_register(url, **kwargs):
        calls.append(url)
        return real_register(url, **kwargs)

    monkeypatch.setattr(telegram, "register_source", counting_register)
    limit = telegram.MAX_SOURCE_ATTEMPTS_PER_DAY
    for update_id in range(1, limit + 2):
        process_update("bot", update(update_id, "/add https://unsupported.example.org/"))

    assert len(calls) == limit
    assert "limit" in reply_for(limit + 1)
    assert SourceRequest.objects.filter(source__isnull=True).count() == limit


# L6: retention purges finished messages and dormant chat IDs.


def test_purge_expired_data(settings):
    settings.DATA_RETENTION_DAYS = 30
    old = timezone.now() - timedelta(days=31)
    active = Subscriber.objects.create(telegram_chat_id="1", active=True)
    stopped = Subscriber.objects.create(telegram_chat_id="2", stopped_at=old)
    never_opted_in = Subscriber.objects.create(telegram_chat_id="3")
    recently_stopped = Subscriber.objects.create(telegram_chat_id="4", stopped_at=timezone.now())
    Subscriber.objects.filter(pk=never_opted_in.pk).update(created_at=old)
    finished = Delivery.objects.create(
        subscriber=active, dedupe_key="old", message="x", status="accepted", finished_at=old
    )
    pending = Delivery.objects.create(subscriber=active, dedupe_key="pending", message="x")

    counts = purge_expired_data()

    assert counts["subscribers"] == 2
    remaining = set(Subscriber.objects.values_list("pk", flat=True))
    assert remaining == {active.pk, recently_stopped.pk}
    assert stopped.pk not in remaining
    assert not Delivery.objects.filter(pk=finished.pk).exists()
    assert Delivery.objects.filter(pk=pending.pk).exists()
