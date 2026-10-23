# Architecture

Event Bank is a modular Django monolith backed by PostgreSQL. There is one codebase, one
database, and one worker process running the same image. There is no message broker; source
claims and the notification outbox use PostgreSQL locking.

## Layout and dependency rules

```
backend/
├── config/            Settings (environment-driven), URL routing
├── connectors/        Pure Python. Connector contract, SafeFetcher, JSON-LD extraction, registry
│   ├── base.py        RawEvent, SourceSpec, Connector, Fetcher protocol
│   ├── http.py        SafeFetcher and URL/DNS validation
│   ├── registry.py    Slug registry, URL matching, autodiscovery of connectors/platforms/
│   ├── extract/       Reusable extractors (jsonld.py)
│   └── platforms/     One module per platform (luma_ical.py: free iCal feeds; luma.py: official API)
├── pipeline/          Pure Python. normalize.py, fingerprint.py, diff.py
├── apps/
│   ├── common/        Health endpoint, DenyAll default permission, log redaction, phone numbers
│   ├── sources/       Source model, register_source(), add_source command, admin review
│   ├── ingestion/     SourceRun model, ingest_source() orchestration and command
│   ├── events/        Event and EventRevision models, upsert_events()
│   ├── subscriptions/ Telegram subscribers, source follows, opt-in/stop commands, polling cursor
│   └── notifications/ Transactional outbox, delivery service, Telegram/mNotify/console providers
└── tests/             Mocked HTTP and DNS only; sockets are blocked during tests
```

Allowed imports point downwards only:

- `connectors` and `pipeline` never import Django. `tests/test_layering.py` enforces this.
- `ingestion` → `sources`, `events`, `notifications`, `connectors`, `pipeline`
- `events` → `pipeline`
- `notifications.services` → `events`, `sources`, `subscriptions.models`
- `subscriptions.telegram` → `sources`, `notifications.models`
- `notifications.providers` → `common`

## Ingestion flow

`ingest_source(source_id)` in `apps/ingestion/services.py`:

1. Refuses sources that are not `approved`.
2. Locks the source briefly and claims a `SourceRun` before fetching. A running claim younger than
   15 minutes rejects overlapping attempts. Older claims are expired and may be replaced.
3. Validates the source's `config` and resolves its credentials from environment variables
   (`Connector.credentials_from_env`). Secrets are never stored in the database.
4. Builds a `FetchPolicy` from the connector's `allowed_hosts` and settings, and gives the connector
   a `SafeFetcher`. Connectors never open connections themselves.
5. Calls `connector.collect()`, capped at 500 events.
6. Normalises with `pipeline.normalize_events()`: cleans text, converts times to UTC (naive times
   use the event's timezone, then the source's), canonicalises URLs (only http/https survive),
   drops invalid events and duplicate external IDs with a recorded reason.
7. Opens one database transaction, locks the `Source` row, rechecks approval and run ownership,
   calls `upsert_events()`, and queues alerts for significant revisions and active source follows.
   An expired run cannot overwrite a newer run even if its network request eventually finishes.
8. Marks the run `succeeded` with counts, or `failed` with an error code and a redacted message.

Network I/O happens outside the transaction. Any exception rolls back all event changes for that run.
A run with zero valid events fails with `no_events`, because that usually means the page format changed.

## Deduplication and change detection

- An event is identified by `(source, external_id)`, enforced by a unique constraint. Connectors
  must supply an `external_id` that is stable across fetches.
- `pipeline.normalize.to_snapshot()` serialises the tracked fields. `fingerprint()` is a SHA-256
  over that snapshot; `diff_snapshots()` lists the changed fields.
- New event: `Event` row plus an `EventRevision` of kind `created` (significant).
- Unchanged: only `last_seen_at` is updated. No revision.
- Changed: fields updated, `last_changed_at` set, and an `EventRevision` of kind `updated` stores
  the changed field names with before/after values.
- Significant fields: `starts_at`, `ends_at`, `is_all_day`, `venue_name`, `address`,
  `attendance_mode`, `status`. Title, description, URL and image edits are recorded but not significant.
- Events missing from a fetch are never deleted or modified.
- Cross-source deduplication (the same event listed on two platforms) is not implemented.

## Free JSON dataset connectors

`agentconfs` and `confstech` use the same ingestion flow. Their descriptions preserve source
attribution/license notices through normalisation and storage. See
[free event sources](free-event-sources.md) for supported URLs, filtering, verification and
identity/date limitations.

## Luma iCal feed connector

`connectors/platforms/luma_ical.py` reads Luma's free iCal subscription feeds, parsed with the
`icalendar` library:

- Registered URLs are the feed links themselves: `https://api.lu.ma/ics/get?entity=calendar&id=cal-…`
  or `entity=discover&id=discplace-…` (also on `api2.luma.com`). Any other path, entity or extra
  query parameter is rejected, which keeps personal token-bearing feeds out. Only those two hosts
  may be fetched, and responses must be `text/calendar`.
- Each `VEVENT` becomes a `RawEvent`. The external ID is the `UID` (plus `RECURRENCE-ID` when
  present), and `STATUS:CANCELLED` maps to `cancelled`. The event URL is the `URL` property, or else
  the first luma.com link in the description or location.
- `CLASS:PRIVATE`/`CONFIDENTIAL` events and events that ended more than 24 hours ago are skipped.
  `ORGANIZER` and `ATTENDEE` are never read, and a location that is a link is not stored.
- `RRULE` recurrences are not expanded.

## Luma API connector

`connectors/platforms/luma.py` uses Luma's official API (Luma Plus):

- Registered URLs are calendar pages (`https://luma.com/<slug>`; `lu.ma` and `www.luma.com` are
  canonicalised to `luma.com`). They identify the calendar and are never fetched. The fetcher may
  only contact `public-api.luma.com` (`allowed_hosts` vs `url_hosts`).
- Each run calls `GET /v1/calendars/get` and fails with `calendar_mismatch` unless the key's
  calendar slug equals the registered slug. Then it pages through `GET /v1/calendars/events/list`
  (`after` = now minus 24 hours, 50 per page, at most 10 pages), sending the key in the
  `x-luma-api-key` header.
- Only events the calendar manages are requested (`access=manage`), and any entry Luma doesn't mark
  `access: "manage"` is dropped. Events managed by other hosts are excluded pending Luma's written
  confirmation that redistributing them is permitted.
- For personal calendars (`is_personal` true or missing), no organiser name is stored, because the
  calendar name may be a person's name.
- Only events with `visibility: public` are kept. If `location_visibility` is not `public`, the venue
  and street address are dropped and only the city is kept. (Luma returns full addresses for events
  the calendar manages, even when the host limited them to guests.) Meeting URLs, registration
  questions and submitter details are never mapped.
- The list endpoint returns no description or cancellation status, so Luma events are stored
  without a description. Luma deletes cancelled events, so cancellation shows up as an event no longer
  being returned, and missing events are never deleted. Detecting cancellations needs a later design
  (e.g. Luma webhooks or per-event lookups).

## Source lifecycle

`pending` → `approved` / `rejected`, and `approved` → `paused`. `add_source` creates pending sources
unless `--approve` is passed by the operator. The admin cannot create sources directly, so every
source passes through `register_source()` validation. Events and runs are read-only in the admin.

## Notification providers

`apps/notifications/providers/base.py` defines `NotificationProvider.send()` returning a
`SendResult` with a `DeliveryOutcome`:

| Outcome | Meaning | Automatic retry |
|---|---|---|
| `accepted` | Provider accepted the message | n/a |
| `rejected` | Provider refused it; retrying will not help | No |
| `retryable` | Request never reached the provider (connect failure) or was rate-limited | Allowed |
| `unknown` | It may have been sent (timeout after sending, 5xx, unparseable reply) | **Never** |

`MNotifyProvider` normalises recipients to Ghana E.164 numbers, sends the national format that
mNotify documents, and classifies responses as above. It refuses to construct a real HTTP client
unless `SMS_SENDING_ENABLED=true`. It is not yet wired to subscriptions. Telegram has its own
provider using the official Bot API, private chat IDs and plain text, with paid broadcasts disabled.

The ingestion transaction creates a unique delivery per subscriber/revision. No HTTP send happens
inside that transaction. A worker claims pending rows with `select_for_update(skip_locked=True)`,
checks opt-in/follow/source status again, commits the claim, and sends. Only explicit retryable
outcomes are retried (three attempts maximum, exponential delay and provider `retry_after`).
Claims abandoned for five minutes become `unknown`; they are never blindly replayed. Accepted
means provider acceptance, not proof of reading or final delivery. This trades possible missed
messages after crashes for avoiding duplicate sends under uncertainty.

`run_worker` takes a PostgreSQL advisory lock to allow one worker per database. It polls Telegram
commands, optionally imports due approved sources, then processes up to 20 deliveries per cycle
with 1.1 seconds between sends. Bot offsets, command changes and replies commit together. Duplicate
updates do not repeat mutations or acknowledgements. Only private-chat users can follow sources;
`/start` alone does not consent to alerts. `/stop` removes follows and cancels pending event alerts.
An already in-flight HTTP request cannot be recalled. See [setup](alert-pipeline.md).

## API

`GET /api/health/`, `/api/events/`, `/api/sources/` and `/api/platforms/` are public. Events and
sources expose only approved sources; source config and subscriber details are not returned.
Events support location and attendance-mode filters and retain attribution descriptions.
`POST /api/sources/submit/` requires Django session authentication and CSRF protection. All
submissions are pending, with a five-source daily limit per account. Telegram has a separate
five-request daily limit per private chat. DRF's default permission is `DenyAll`; every view
must explicitly declare its permissions. Requests are throttled by default.

## Not yet built

Frontend, WhatsApp adapter/templates/opt-in, SMS subscriber verification and STOP routing,
cross-source deduplication, generalized scraping, and reliable cancellation reconciliation for
sources that silently remove events. The current worker is intended for small deployments.
