# Event Bank

An open-source, self-hostable platform that collects technology events from public sources,
normalises them, detects new and changed events, and (in later phases) notifies subscribers by
SMS, Telegram and WhatsApp.

## Status

| Area | State |
|---|---|
| Source registration (`add_source`), admin approval | Implemented |
| SSRF-hardened fetching (`SafeFetcher`) | Implemented |
| schema.org JSON-LD extraction | Implemented |
| Normalisation, fingerprinting, change detection | Implemented |
| Transactional, idempotent ingestion (`ingest_source`) | Implemented |
| Luma connector (official API) | Implemented and tested against synthetic fixtures; not yet run against the live API |
| Verified event import (`import_events`) | Implemented |
| Read-only events API (`GET /api/events/`) | Implemented |
| mNotify SMS provider client | Implemented, not wired to any workflow; real sending disabled by default |
| Scheduling, events page, subscribers, notification delivery | Not started (later phases) |

To add another platform, see [docs/adding-a-connector.md](docs/adding-a-connector.md). To add
events confirmed with organisers, see [docs/verified-events.md](docs/verified-events.md).

## Events API

Public and read-only. `GET /api/events/` lists upcoming, non-cancelled events from approved
sources, soonest first, 25 per page. `GET /api/events/<id>/` returns one event.

| Parameter | Values |
|---|---|
| `when` | `upcoming` (default: not yet ended), `past`, `all` |
| `starts_after`, `starts_before` | `YYYY-MM-DD`, inclusive, in the server timezone |
| `location` | Text matched against venue and address, e.g. `Accra` |
| `attendance` | `online` (online or hybrid), `in_person` (in person or hybrid) |
| `include_cancelled` | `true` to include cancelled events |
| `source` | Source ID |
| `page`, `page_size` | `page_size` up to 100 |

Each event has `url` (the registration or official page) and a `source` object with `name`,
`platform`, `url` and `last_checked_at`, the source's last successful ingestion. `last_seen_at` is
when the event was last confirmed by its source. Invalid parameters return 400. Anonymous requests
are throttled (60 per minute by default).

## Luma calendars

Event Bank reads Luma only through Luma's official API. It never scrapes luma.com pages: Luma's
Terms of Use only allow access through its publicly supported interfaces. Requirements:

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
Event Bank has not yet been run against the live Luma API: do not run `ingest_source` on a Luma
source until Luma has confirmed that your intended use is permitted.

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

Event Bank is licensed under the GNU Affero General Public License v3.0 only
(SPDX: `AGPL-3.0-only`); see [LICENSE](LICENSE). If you run a modified version as a network
service, you must offer its source code to that service's users.
