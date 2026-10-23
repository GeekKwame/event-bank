# Architecture

Event Bank is a modular Django monolith backed by PostgreSQL. There is one codebase, one
database, and (from Phase 2) one worker process running the same image. There is no message
broker; background work will use PostgreSQL row locking.

## Layout and dependency rules

```
backend/
├── config/            Settings (environment-driven), URL routing
├── connectors/        Pure Python. Connector contract, SafeFetcher, JSON-LD extraction, registry
│   ├── base.py        RawEvent, SourceSpec, Connector, Fetcher protocol
│   ├── http.py        SafeFetcher and URL/DNS validation
│   ├── registry.py    Slug registry, URL matching, autodiscovery of connectors/platforms/
│   ├── extract/       Reusable extractors (jsonld.py)
│   └── platforms/     One module per supported platform (luma.py: Luma official API)
├── pipeline/          Pure Python. normalize.py, fingerprint.py, diff.py
├── apps/
│   ├── common/        Health endpoint, DenyAll default permission, log redaction, phone numbers
│   ├── sources/       Source model, register_source(), add_source command, admin review
│   ├── ingestion/     SourceRun model, ingest_source() orchestration and command
│   ├── events/        Event and EventRevision models, upsert_events()
│   ├── subscriptions/ Placeholder for subscribers and consent (Phase 3)
│   └── notifications/ providers/: provider contract, mNotify, console (dev)
└── tests/             Mocked HTTP and DNS only; sockets are blocked during tests
```

Allowed imports point downwards only:

- `connectors` and `pipeline` never import Django. `tests/test_layering.py` enforces this.
- `ingestion` → `sources`, `events`, `connectors`, `pipeline`
- `events` → `pipeline`
- `notifications.providers` → `common`

## Ingestion flow

`ingest_source(source_id)` in `apps/ingestion/services.py`:

1. Refuses sources that are not `approved`.
2. Creates a `SourceRun` (status `running`) before any network I/O, so failures are always recorded.
3. Validates the source's `config` and resolves its credentials from environment variables
   (`Connector.credentials_from_env`). Secrets are never stored in the database.
4. Builds a `FetchPolicy` from the connector's `allowed_hosts` and settings, and gives the connector
   a `SafeFetcher`. Connectors never open connections themselves.
5. Calls `connector.collect()`, capped at 500 events.
6. Normalises with `pipeline.normalize_events()`: cleans text, converts times to UTC (naive times
   use the event's timezone, then the source's), canonicalises URLs (only http/https survive),
   drops invalid events and duplicate external IDs with a recorded reason.
7. Opens one database transaction, locks the `Source` row (serialising concurrent runs of the same
   source), and calls `upsert_events()`.
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

## Luma connector

`connectors/platforms/luma.py` uses Luma's official API only:

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
unless `SMS_SENDING_ENABLED=true`. No workflow sends notifications yet, and no retry logic exists.
Telegram and WhatsApp will be separate provider modules.

## API

DRF's default permission is `DenyAll`, so every view must declare `permission_classes`;
`tests/test_common.py` fails if one does not. Anonymous and authenticated requests are throttled
by default.

- `GET /api/health/`: public health check.
- `GET /api/events/` and `GET /api/events/<id>/` (`apps/events/api.py`): public and read-only, with
  no authentication or CSRF. Only events from `approved` sources are published; pausing or rejecting
  a source hides its events. Filters are validated by `EventFilterSerializer`, and invalid values
  return 400. Each source's last successful run is added with one subquery, so a page costs a
  constant number of queries. "Upcoming" means `ends_at` is in the future, or there is no `ends_at`
  and `starts_at` is in the future; events without a start time are never listed.

## Verified event imports

`import_events` (`apps/ingestion/imports.py`) loads a JSON file of operator-verified events into a
source with connector `manual`, which is created approved and never fetched (`ingest_source`
refuses it). The file is validated strictly before anything is written. The events then go
through the same `run_ingestion()` path as fetched sources: one `SourceRun` (trigger `import`),
normalisation, and a single transaction for upserts and revisions. See `docs/verified-events.md`.

## Not yet built

Scheduling worker, events page, subscribers and consent, notification outbox, delivery tracking
and retries, Telegram and WhatsApp.

Known issue to fix before scheduling: two runs of the same source fetch outside the transaction,
so a slower run that fetched earlier could commit after a newer run and overwrite fresher data.
A valid but empty source also fails with `no_events`, the same as an extraction failure.
