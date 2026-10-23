from datetime import timedelta

import httpx
import pytest
from django.db import transaction
from django.utils import timezone

from apps.events.models import Event, EventRevision
from apps.ingestion.models import SourceRun
from apps.ingestion.services import IngestionError, ingest_source
from apps.ingestion.worker import ingest_due_sources
from apps.notifications.models import Delivery
from apps.notifications.providers.base import DeliveryOutcome, SendResult
from apps.notifications.services import deliver_one, enqueue_run_alerts
from apps.sources.models import Source
from apps.subscriptions.models import Subscriber, Subscription, TelegramCursor
from apps.subscriptions.telegram import process_update
from tests.conftest import EXAMPLE_SOURCE_URL, jsonld_page

pytestmark = pytest.mark.django_db


@pytest.fixture
def followed_source(example_connector, fake_site, base_events):
    for event in base_events:
        event["startDate"] = (timezone.now() + timedelta(days=3)).isoformat()
        event["endDate"] = (timezone.now() + timedelta(days=3, hours=2)).isoformat()
    fake_site.html = jsonld_page(base_events)
    source = Source.objects.create(
        url=EXAMPLE_SOURCE_URL, connector="example", status=Source.Status.APPROVED
    )
    subscriber = Subscriber.objects.create(
        telegram_chat_id="12345", active=True, consented_at=timezone.now()
    )
    Subscription.objects.create(subscriber=subscriber, source=source)
    return source, subscriber


class StubProvider:
    def __init__(self, outcome=DeliveryOutcome.ACCEPTED, retry_after=None):
        self.outcome = outcome
        self.retry_after = retry_after
        self.calls = []

    def send(self, recipients, message):
        self.calls.append((recipients, message))
        return SendResult(provider="telegram", outcome=self.outcome, retry_after=self.retry_after)


def update(update_id, text, chat_id=12345, kind="private"):
    return {
        "update_id": update_id,
        "message": {
            "chat": {"type": kind, "id": chat_id},
            "from": {"id": chat_id, "is_bot": False},
            "text": text,
        },
    }


def test_ingestion_queues_once_and_delivers(followed_source, fake_site):
    source, subscriber = followed_source
    run = ingest_source(source.pk, fetcher_factory=fake_site.fetcher_factory)
    assert run.status == "succeeded"
    assert Delivery.objects.count() == 2
    with transaction.atomic():
        enqueue_run_alerts(run)
    ingest_source(source.pk, fetcher_factory=fake_site.fetcher_factory)
    assert Delivery.objects.count() == 2
    provider = StubProvider()
    sent = deliver_one(provider)
    assert sent.status == "accepted" and sent.attempts == 1
    assert provider.calls[0][0] == [subscriber.telegram_chat_id]
    assert "When:" in provider.calls[0][1] and "Where:" in provider.calls[0][1]
    assert "https://events.example.org/" in provider.calls[0][1]


@pytest.mark.django_db(transaction=True)
def test_provider_runs_outside_transaction_and_claim_prevents_duplicate_send():
    subscriber = Subscriber.objects.create(telegram_chat_id="12345")
    Delivery.objects.create(subscriber=subscriber, dedupe_key="one", message="Example")

    class CheckingProvider(StubProvider):
        def send(self, recipients, message):
            assert not transaction.get_connection().in_atomic_block
            assert deliver_one(StubProvider()) is None
            return super().send(recipients, message)

    provider = CheckingProvider()
    assert deliver_one(provider).status == "accepted"
    assert len(provider.calls) == 1


def test_dataset_credit_and_long_links_survive_alert_rendering(followed_source, fake_site):
    from apps.notifications.services import render_alert
    from connectors.platforms.confstech import ATTRIBUTION, DATA_LICENSE, LICENSE_URL

    source, _ = followed_source
    ingest_source(source.pk, fetcher_factory=fake_site.fetcher_factory)
    event = Event.objects.get(external_id="evt-meetup")
    event.attribution = ATTRIBUTION
    event.url = "https://example.org/" + "a" * 1900
    event.title = "🙂" * 300
    event.save()
    revision = event.revisions.select_related("event__source").get()
    message = render_alert(revision)
    assert ATTRIBUTION in message and LICENSE_URL in message and event.url in message
    assert DATA_LICENSE not in message
    assert len(message.encode("utf-16-le")) // 2 <= 4096


def test_outbox_failure_rolls_back_events(followed_source, fake_site, monkeypatch):
    source, _ = followed_source

    def fail(run):
        raise RuntimeError("outbox unavailable")

    monkeypatch.setattr("apps.ingestion.services.enqueue_run_alerts", fail)
    run = ingest_source(source.pk, fetcher_factory=fake_site.fetcher_factory)
    assert run.status == "failed"
    assert not Event.objects.exists() and not EventRevision.objects.exists()
    assert not Delivery.objects.exists()


def test_only_significant_updates_queue_and_replace_stale_alerts(
    followed_source, fake_site, base_events
):
    source, _ = followed_source
    ingest_source(source.pk, fetcher_factory=fake_site.fetcher_factory)
    base_events[0]["description"] = "Edited description"
    fake_site.html = jsonld_page(base_events)
    ingest_source(source.pk, fetcher_factory=fake_site.fetcher_factory)
    assert Delivery.objects.count() == 2
    base_events[0]["location"]["name"] = "New Hall"
    fake_site.html = jsonld_page(base_events)
    ingest_source(source.pk, fetcher_factory=fake_site.fetcher_factory)
    assert Delivery.objects.count() == 3
    assert Delivery.objects.filter(status="cancelled", error_code="superseded").count() == 1
    assert "New Hall" in Delivery.objects.latest("pk").message


def test_past_or_undated_events_do_not_alert(followed_source, fake_site, base_events):
    source, _ = followed_source
    base_events[0]["startDate"] = "2020-01-01"
    base_events[0]["endDate"] = "2020-01-02"
    base_events[1].pop("startDate")
    base_events[1].pop("endDate")
    fake_site.html = jsonld_page(base_events)
    ingest_source(source.pk, fetcher_factory=fake_site.fetcher_factory)
    assert Event.objects.count() == 2
    assert not Delivery.objects.exists()


@pytest.mark.parametrize("reason", ["inactive", "unfollowed", "paused"])
def test_delivery_rechecks_eligibility(followed_source, fake_site, reason):
    source, subscriber = followed_source
    ingest_source(source.pk, fetcher_factory=fake_site.fetcher_factory)
    if reason == "inactive":
        subscriber.active = False
        subscriber.save()
    elif reason == "unfollowed":
        subscriber.subscriptions.all().delete()
    else:
        source.review(Source.Status.PAUSED)
    provider = StubProvider()
    assert deliver_one(provider).status == "cancelled"
    assert not provider.calls


@pytest.mark.parametrize("outcome", [DeliveryOutcome.UNKNOWN, DeliveryOutcome.REJECTED])
def test_uncertain_or_rejected_delivery_never_retries(followed_source, outcome):
    _, subscriber = followed_source
    delivery = Delivery.objects.create(subscriber=subscriber, dedupe_key="one", message="Example")
    provider = StubProvider(outcome)
    assert deliver_one(provider).status == outcome.value
    assert deliver_one(provider) is None
    delivery.refresh_from_db()
    assert delivery.attempts == 1


def test_retry_after_and_max_attempts(followed_source):
    _, subscriber = followed_source
    delivery = Delivery.objects.create(subscriber=subscriber, dedupe_key="one", message="Example")
    provider = StubProvider(DeliveryOutcome.RETRYABLE, retry_after=300)
    now = timezone.now()
    assert deliver_one(provider).status == "pending"
    delivery.refresh_from_db()
    assert delivery.available_at >= now + timedelta(seconds=300)
    assert deliver_one(provider) is None
    for _ in range(2):
        Delivery.objects.filter(pk=delivery.pk).update(available_at=timezone.now())
        deliver_one(provider)
    delivery.refresh_from_db()
    assert delivery.attempts == 3 and delivery.status == "rejected"
    assert delivery.error_code == "retry_exhausted"


def test_crashed_send_becomes_unknown(followed_source):
    _, subscriber = followed_source
    delivery = Delivery.objects.create(
        subscriber=subscriber,
        dedupe_key="one",
        message="Example",
        status="sending",
        claimed_at=timezone.now() - timedelta(minutes=10),
        attempts=1,
    )
    provider = StubProvider()
    assert deliver_one(provider) is None
    delivery.refresh_from_db()
    assert delivery.status == "unknown" and not provider.calls


def test_opt_in_stop_and_duplicate_updates(followed_source, fake_site):
    source, subscriber = followed_source
    ingest_source(source.pk, fetcher_factory=fake_site.fetcher_factory)
    assert process_update("test-bot", update(10, "/stop"))
    assert not process_update("test-bot", update(10, "/stop"))
    subscriber.refresh_from_db()
    assert not subscriber.active and not subscriber.subscriptions.exists()
    assert Delivery.objects.filter(revision__isnull=False, status="cancelled").count() == 2
    assert Delivery.objects.filter(revision__isnull=True).count() == 1
    process_update("test-bot", update(11, f"/follow {source.pk}"))
    subscriber.refresh_from_db()
    assert subscriber.active and subscriber.consented_at is not None
    assert subscriber.subscriptions.count() == 1
    assert TelegramCursor.objects.get(bot_id="test-bot").next_update_id == 12


def test_start_does_not_opt_in_and_groups_are_ignored():
    process_update("test-bot", update(1, "/start"))
    assert not Subscriber.objects.get(telegram_chat_id="12345").active
    process_update("test-bot", update(2, "/start", chat_id=-12345, kind="group"))
    assert Subscriber.objects.count() == 1 and Delivery.objects.count() == 1


def test_submit_supported_url_pending_and_no_fetch(example_connector, monkeypatch):
    monkeypatch.setattr("apps.sources.services.validate_fetch_target", lambda *a, **kw: None)
    process_update("test-bot", update(1, f"/add {EXAMPLE_SOURCE_URL}"))
    source = Source.objects.get()
    assert source.status == "pending" and source.connector == "example"
    assert not SourceRun.objects.exists()
    assert not Subscription.objects.exists()
    assert Subscriber.objects.get().source_requests.count() == 1


def test_running_source_is_not_fetched_twice(followed_source, fake_site):
    source, _ = followed_source
    SourceRun.objects.create(source=source)
    with pytest.raises(IngestionError, match="already"):
        ingest_source(source.pk, fetcher_factory=fake_site.fetcher_factory)
    assert not fake_site.requests


def test_pausing_during_fetch_prevents_writes(followed_source, fake_site):
    source, _ = followed_source

    def pause(request):
        source.review(Source.Status.PAUSED)
        return httpx.Response(200, html=fake_site.html)

    fake_site.response = pause
    run = ingest_source(source.pk, fetcher_factory=fake_site.fetcher_factory)
    assert run.status == "failed" and run.error_code == "source_not_approved"
    assert not Event.objects.exists() and not Delivery.objects.exists()


def test_expired_old_run_cannot_overwrite_newer_results(followed_source, fake_site, base_events):
    source, _ = followed_source
    old_html = fake_site.html

    def newer_run(request):
        SourceRun.objects.filter(source=source, status="running").update(
            started_at=timezone.now() - timedelta(minutes=20)
        )
        base_events[0]["name"] = "Newer version"
        fake_site.html = jsonld_page(base_events)
        fake_site.response = None
        newer = ingest_source(source.pk, fetcher_factory=fake_site.fetcher_factory)
        assert newer.status == "succeeded"
        return httpx.Response(200, html=old_html)

    fake_site.response = newer_run
    older = ingest_source(source.pk, fetcher_factory=fake_site.fetcher_factory)
    assert older.error_code == "run_superseded"
    assert Event.objects.get(external_id="evt-meetup").title == "Newer version"
    assert EventRevision.objects.count() == 2 and Delivery.objects.count() == 2


def test_scheduler_opt_in_and_interval(followed_source, fake_site, settings):
    source, _ = followed_source
    calls = []

    def ingest(pk, **kwargs):
        calls.append(pk)
        return ingest_source(pk, fetcher_factory=fake_site.fetcher_factory, **kwargs)

    settings.INGESTION_SCHEDULING_ENABLED = False
    assert ingest_due_sources(ingest=ingest) == 0
    settings.INGESTION_SCHEDULING_ENABLED = True
    assert ingest_due_sources(ingest=ingest) == 1
    assert ingest_due_sources(ingest=ingest) == 0
    assert calls == [source.pk]
