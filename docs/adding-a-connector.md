# Adding a connector

A connector reads events from one platform and returns them as `RawEvent` objects. The pipeline
handles normalisation, deduplication, change detection and storage, so a connector never touches
the database and needs no changes to core code.

## Before you start

- Check the platform's terms of use. Prefer an official API, feed (e.g. iCal) or embed over
  scraping HTML. Some terms only permit access through "publicly supported interfaces".
- Never bypass logins, CAPTCHAs, rate limits or other access controls.
- Only collect public event information. Never collect attendee or guest data.

## 1. Write the connector

Create `backend/connectors/platforms/<platform>.py`. Modules in `connectors/platforms/` are
discovered automatically.

```python
from connectors.base import Connector
from connectors.extract.jsonld import find_events, text_value, to_raw_event
from connectors.registry import register


@register
class ExamplePlatformConnector(Connector):
    slug = "example-platform"
    name = "Example Platform"
    allowed_hosts = frozenset({"events.example-platform.com"})

    @classmethod
    def matches_path(cls, path: str) -> bool:
        return path.startswith("/e/")

    def collect(self, source, fetcher):
        page = fetcher.get(source.url)
        for node in find_events(page.text):
            yield to_raw_event(
                node,
                external_id=text_value(node.get("@id")) or page.url,
                base_url=page.url,
            )
```

Rules:

- **No Django imports** in `connectors/`. `tests/test_layering.py` fails if any are added.
- **No direct network access.** Use only the `fetcher` passed to `collect()`. It enforces https,
  your `allowed_hosts`, public-IP-only DNS, IP pinning, redirect re-validation (max 3), a 2 MiB
  response limit and a 20 second total deadline.
- **`allowed_hosts` is the allowlist** for registration and for every redirect hop. Keep it to the
  exact hostnames you need.
- **`external_id` must be stable** across fetches of the same event. Prefer a platform event ID over
  a URL slug that organisers can rename. It is the deduplication key within a source.
- `slug` must be unique; registration fails on duplicates. A URL matching more than one connector
  is rejected as ambiguous.
- Return naive datetimes only when the source gives local times without an offset; they are
  interpreted in the event or source timezone. Return a `date` for all-day events.
- Raise `connectors.base.ConnectorError` when the content cannot be interpreted.

### Configuration and credentials

- Per-source settings live in `Source.config`, set with `add_source --config KEY=VALUE`. Override
  `validate_config()` to accept and clean them; by default a connector takes no config.
- Secrets never go in `config` or the database. Override `credentials_from_env(config, environ)`
  to read them from environment variables. The result reaches `collect()` as
  `source.credentials`, which is excluded from `repr()`. Limit which variable names a config may
  reference (Luma only allows `LUMA_API_KEY` and `LUMA_API_KEY_*`), so a config can't point at
  unrelated secrets.
- Send credentials in headers: `fetcher.get(url, headers={...})`. `SafeFetcher` sends extra headers
  only to the original host and drops them on redirects to a different host. Headers it manages
  (`Host`, `Cookie`, `User-Agent`, `Accept-Encoding`, ...) cannot be overridden.
- If registered URLs differ from the hosts you fetch (e.g. a calendar page vs an API host), set
  `url_hosts` for registration matching and keep `allowed_hosts` to the hosts actually contacted.
  `connectors/platforms/luma.py` is a complete example.

### Privacy

Only map fields that are meant to be public. Skip non-public events. Never map attendee data,
meeting links or the contact details of submitters.

## 2. Test it without the network

Tests must not make live requests; sockets are blocked during the test run. Use a fixture file and
`httpx.MockTransport` through `SafeFetcher`, as `tests/conftest.py` (`FakeSite`) and
`tests/test_ingestion.py` do.

Fixture rules:

- Do not commit copies of third-party pages unless their terms allow it. Prefer a minimal
  hand-written fixture that reproduces only the structure your parser relies on, and say so in a
  comment (see `tests/fixtures/schema_org_events.html`).
- Remove personal data (names, emails, phone numbers) from any fixture.

Cover at least: parsing a representative page, a stable `external_id`, missing optional fields,
and URL matching (`matches()`) for accepted and rejected URLs.

## 3. Try it locally

```bash
docker compose exec backend python manage.py add_source <url> --approve
docker compose exec backend python manage.py ingest_source <source-id>
```

Running `ingest_source` makes a real request. Do it rarely, and only against pages you are allowed to access.
