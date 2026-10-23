# Event Bank

A platform that collects technology events from public sources, normalises them, detects new
and changed events, and (in later phases) notifies subscribers by SMS, Telegram and WhatsApp.
The source code is public, but the project currently has no license (see [License](#license)).

## Status

| Area | State |
|---|---|
| Source registration (`add_source`), admin approval | Implemented |
| SSRF-hardened fetching (`SafeFetcher`) | Implemented |
| schema.org JSON-LD extraction | Implemented |
| Normalisation, fingerprinting, change detection | Implemented |
| Transactional, idempotent ingestion (`ingest_source`) | Implemented |
| Luma iCal feed connector (free) | Implemented and tested against synthetic fixtures; not yet run against live feeds |
| Luma connector (official API, Luma Plus) | Implemented and tested against synthetic fixtures; not yet run against the live API |
| mNotify SMS provider client | Implemented, not wired to any workflow; real sending disabled by default |
| Scheduling, public API, subscribers, notification delivery | Not started (later phases) |

To add another platform, see [docs/adding-a-connector.md](docs/adding-a-connector.md).

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
- [Adding a connector](docs/adding-a-connector.md)
- [Security policy and threat model](SECURITY.md)
- [Contributing](CONTRIBUTING.md)

## License

Event Bank currently has no license. The source code is publicly visible, but without a license
no one else is granted permission to use, copy, modify, host or distribute it, so it is not open
source. A project license does not govern which third-party sites Event Bank may access; their
own terms do.

Commits up to and including `2b23819` were published with the GNU Affero General Public License
v3.0 only (`AGPL-3.0-only`), and copies obtained from those commits carry that license.
