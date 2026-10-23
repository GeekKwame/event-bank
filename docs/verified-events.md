# Verified events (organiser submissions)

Use this when a source can't be fetched automatically: events confirmed directly with organisers,
or a community list you maintain. Only import events you have verified with the organiser, and
only details the organiser has agreed to publish.

```bash
docker compose cp events.json backend:/tmp/events.json
docker compose exec backend python manage.py import_events /tmp/events.json \
    --source-url https://organiser.example.org/ --source-name "Organiser name"
```

- `--source-url` identifies who supplied the events (e.g. the organiser's website). It must be
  `https://`. The first import creates an approved source with connector `manual`; later imports
  with the same URL update it. A URL already used by a fetched source is rejected.
- `--timezone` sets the timezone for times without an offset (default `Africa/Accra`).
- Re-importing the same file changes nothing. Edited events get a revision, and start time, venue
  or status changes are marked significant. Events missing from a later file are **not** deleted:
  set `"status": "cancelled"` instead.
- The whole file is rejected before anything is written if it isn't valid JSON, has unknown or
  missing fields, or has bad dates. Events with unsafe links (anything but `http(s)`) or duplicate
  IDs are skipped and reported.
- `ingest_source` refuses manual sources.

## File format

A JSON array (maximum 500 events, 2 MiB):

```json
[
  {
    "id": "devfest-2026",
    "title": "Event title",
    "url": "https://organiser.example.org/register",
    "starts_at": "2026-11-14T09:00:00",
    "ends_at": "2026-11-14T17:00:00",
    "timezone": "Africa/Accra",
    "venue_name": "Venue name",
    "address": "Street, Accra",
    "attendance_mode": "offline",
    "status": "scheduled",
    "organizer_name": "Organiser name",
    "description": "Short description",
    "image_url": "https://organiser.example.org/cover.png"
  }
]
```

| Field | Required | Notes |
|---|---|---|
| `id` | yes | Stable and unique within this source; reuse it to update the event |
| `title`, `url` | yes | `url` is the registration or official event page |
| `starts_at` | yes | ISO 8601. `YYYY-MM-DD` means an all-day event |
| `ends_at` | no | Same format as `starts_at` |
| `timezone` | no | IANA name; overrides `--timezone` for this event |
| `attendance_mode` | no | `offline`, `online` or `mixed` |
| `status` | no | `scheduled` (default), `cancelled`, `postponed`, `rescheduled`, `moved_online` |
| others | no | `venue_name`, `address`, `organizer_name`, `description`, `image_url` |

Don't include personal contact details (phone numbers, personal emails) in any field: everything
imported is published through the events API.
