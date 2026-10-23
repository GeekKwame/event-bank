# Event Bank

A self-hostable, AGPL-3.0-only platform that collects technology events from supported public
sources, normalises them, detects new and changed events, and queues alerts for opted-in
Telegram subscribers. WhatsApp and SMS delivery workflows are planned extensions.

Users submit a source URL, an operator reviews it, and platform connectors extract event links,
dates and locations. Contributors can add new connectors without changing the ingestion pipeline.
The software needs no paid scraping or AI service; external messaging and hosting can have costs.

## Status

| Area | State |
|---|---|
| Source registration (`add_source`), admin approval | Implemented |
| SSRF-hardened fetching (`SafeFetcher`) | Implemented |
| schema.org JSON-LD extraction | Implemented |
| Normalisation, fingerprinting, change detection | Implemented |
| Transactional, idempotent ingestion (`ingest_source`) | Implemented |
| confs.tech public JSON connector (free) | Implemented; live fetch and normalisation verified |
| AgentConfs public JSON connector (free) | Implemented against synthetic fixtures; direct live access returned HTTP 403 |
| Luma iCal feed connector (free) | Implemented and tested against synthetic fixtures; not yet run against live feeds |
| Luma connector (official API, Luma Plus) | Implemented and tested against synthetic fixtures; not yet run against the live API |
| Source submission and public events/platforms API | Implemented; submissions require login or a private Telegram chat |
| Telegram follows, opt-in and `/stop` | Implemented; tested with mocked Telegram responses |
| Transactional alert outbox and bounded retries | Implemented; uncertain sends are never retried |
| PostgreSQL scheduling/delivery worker | Implemented; scheduling and real Telegram sending disabled by default |
| mNotify SMS provider client | Implemented, not wired to a subscriber workflow; real sending disabled by default |
| WhatsApp, SMS subscriber workflow, frontend | Not implemented |

To add another platform, see [docs/adding-a-connector.md](docs/adding-a-connector.md).

For the complete URL → event → Telegram setup, see [running the alert pipeline](docs/alert-pipeline.md).
No real Telegram messages have been sent during development verification.

## Free JSON event sources

No API key or paid subscription is needed for these connectors. Start with one confs.tech
topic/year file:

```bash
docker compose exec backend python manage.py add_source \
    https://raw.githubusercontent.com/tech-conferences/conference-data/main/conferences/2026/python.json \
    --name "confs.tech Python 2026" --timezone UTC --approve
# Use the source ID printed above:
docker compose exec backend python manage.py ingest_source <source-id>
```

AgentConfs can be registered with `add_source https://agentconfs.com/data/events.json --approve`.
Direct requests from the development environment returned HTTP 403; its live ingestion is not
verified. Neither connector bypasses access restrictions. Both support `--config country=Ghana`
for an exact, case-insensitive country filter; local coverage is not guaranteed.

See [free event sources](docs/free-event-sources.md) for attribution requirements, date/identity
limitations, and how to select confs.tech topics. Scheduling is opt-in through `run_worker`.

## Luma calendars

Event Bank never scrapes luma.com pages: Luma's Terms of Use only allow access through its
publicly supported interfaces. It supports two of them.

**Do not ingest live Luma data yet.** Luma has not yet confirmed in writing that storing events and
sending notifications about them is permitted. Neither connector has been run against Luma.

### Free: iCal subscription feeds

Luma offers free iCal feeds for any public calendar and for Discover city pages
([iCal Syncing](https://help.luma.com/p/ical-syncing)), with no API key or Luma Plus needed. On the
calendar or city page, click "Add iCal Subscription", copy the link, and change `webcal://` to
`https://`:

```bash
docker compose exec backend python manage.py add_source \
    "https://api.lu.ma/ics/get?entity=calendar&id=cal-..." --approve
```

- Only `entity=calendar` and `entity=discover` (city) feeds are accepted. Personal feeds
  (`entity=user`) carry a private token and are rejected, as are links with any other parameters.
- Accepted hosts are `api.lu.ma` and `api2.luma.com`. These are the hosts Luma's subscription links
  have been observed to use; Luma does not document them. If your link uses another host, report it
  rather than editing the allowlist casually.
- Events ending more than 24 hours ago are skipped. `PRIVATE`/`CONFIDENTIAL` events are skipped.
  Organisers and attendees are never read. A location that is a link (such as a meeting URL) is not
  stored. Feed cancellations (`STATUS:CANCELLED`) are detected as significant changes.
- Recurring events are not expanded: only each event's own date is stored.
- Calendar apps poll these feeds every 15 minutes to 24 hours; Event Bank should poll no more than
  hourly once scheduling exists.

### Luma Plus: official API

Requirements:

- A Luma Plus subscription and a calendar API key from `luma.com/calendar/manage/api-keys`.
  Each key is scoped to one calendar, and it **grants full access to that calendar**. Treat it like a
  password, and use it only on calendars you are allowed to manage.
- The key goes in an environment variable, never in the database: `LUMA_API_KEY`, or
  `LUMA_API_KEY_<NAME>` when you have several calendars.

```bash
# .env
LUMA_API_KEY_ACCRA=...

docker compose exec backend python manage.py add_source https://luma.com/<calendar-slug> \
    --config api_key_env=LUMA_API_KEY_ACCRA --approve
```

Each run checks that the key belongs to the registered calendar and fetches upcoming events
(from 24 hours ago onwards). Only `public` events are stored. When a host shows the address
only to guests, only the city is kept. Meeting links and submitter details are never stored.
For personal calendars, the calendar name (which may be a person's name) is not stored as the
organiser.

Only events the calendar itself manages are collected. Events listed on the calendar but managed
by other hosts are excluded until Luma confirms in writing that redistributing them is permitted.

## Stack

Python 3.13, Django + Django REST Framework, PostgreSQL 17, HTTPX, Beautiful Soup,
`uv` for dependency locking, Docker Compose for local development, GitHub Actions for CI.

## Getting started

```bash
cp .env.example .env          # optional: Compose falls back to development defaults
docker compose up --build
curl http://127.0.0.1:8000/api/health/
```

If ports 8000 or 5432 are already in use, set `BACKEND_HOST_PORT` / `POSTGRES_HOST_PORT` in
`.env`. Ports are bound to `127.0.0.1` only.

## Commands

```bash
docker compose exec backend pytest
docker compose exec backend ruff check .
docker compose exec backend ruff format .
docker compose exec backend python manage.py makemigrations
docker compose exec backend python manage.py createsuperuser

# Register a source (pending until approved), or approve immediately as the operator:
docker compose exec backend python manage.py add_source https://example.org/events --approve
# Fetch an approved source once and store its events:
docker compose exec backend python manage.py ingest_source <source-id>
```

Sources, ingestion runs, events and their revision history are visible in the Django admin at
`/admin/`. Pending sources are approved there with the "Approve selected sources" action.

## Dependencies

Dependencies are declared in `backend/pyproject.toml` and locked in `backend/uv.lock`.
After changing dependencies, run `uv lock` in `backend/` and commit the lock file.

## Documentation

- [Architecture](docs/architecture.md)
- [Running the alert pipeline](docs/alert-pipeline.md)
- [Adding a connector](docs/adding-a-connector.md)
- [Security policy and threat model](SECURITY.md)
- [Contributing](CONTRIBUTING.md)

## License

Event Bank is licensed under the GNU Affero General Public License version 3 only
(`AGPL-3.0-only`); see [LICENSE](LICENSE). You may use, modify, host and redistribute it under
that license. Operators of modified versions must offer their corresponding source to users
interacting with those versions over a network. Third-party data retains its own license, and
platform access and messaging charges are governed by the respective providers.
