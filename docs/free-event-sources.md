# Free event sources

These connectors read published JSON datasets through `SafeFetcher`; they do not scrape HTML,
require credentials, or call an AI service. They reuse the normalisation, revision and ingestion
pipeline. The optional worker and public event API are described in
[running the alert pipeline](alert-pipeline.md).

## confs.tech

- Data: https://github.com/tech-conferences/conference-data/tree/main/conferences
- License: https://github.com/tech-conferences/conference-data/blob/main/LICENSE.md (MIT)
- Connector slug: `confstech`

Choose a year and an existing topic filename in the upstream repository. Register its raw URL:

```bash
docker compose exec backend python manage.py add_source \
    https://raw.githubusercontent.com/tech-conferences/conference-data/main/conferences/2026/python.json \
    --name "confs.tech Python 2026" --timezone UTC --approve
docker compose exec backend python manage.py ingest_source <source-id>
```

Only the `tech-conferences/conference-data` repository's `main` branch and
`conferences/20YY/<topic>.json` paths match this connector. The raw `refs/heads/main` spelling
is canonicalised to `main`. Topic files are registered separately; there is no GitHub API crawling
or token requirement. Raw GitHub responses use `text/plain`, which is accepted for this connector.

The source supplies no stable IDs. Event Bank uses a hash of the event URL and start date to
distinguish multiple occurrences at the same URL. Title, venue/city, and end-date changes update
the existing record. **Changing the start date or URL creates a new record.** The old record
remains under the existing missing-event policy. Do not promise reliable rescheduling alerts from
this source. An event in two topic files also remains two source records; cross-source deduplication
is not implemented. Rows without a URL or start date are skipped as missing an identity.

## AgentConfs

- Feed: https://agentconfs.com/data/events.json
- Reuse statement: https://agentconfs.com/about (CC BY 4.0)
- Connector slug: `agentconfs`

```bash
docker compose exec backend python manage.py add_source \
    https://agentconfs.com/data/events.json --name AgentConfs --timezone UTC --approve
docker compose exec backend python manage.py ingest_source <source-id>
```

The homepage URL is also accepted and canonicalised to the feed. Only `agentconfs.com` is
allowlisted. Event fields follow the published feed; both a JSON array and an `events` array
envelope are supported. The source's `id` is the event identity, so date and URL edits can be
tracked. Records with `dates_to_be_confirmed: true` are stored without dates and labelled tentative.

**Live limitation:** the feed was readable through web research, but a direct request from the
development environment returned HTTP 403. The connector is tested with synthetic fixtures, not
a successful live ingestion. A 403 is recorded as a failed run without changing stored events;
there is no fallback that bypasses it.

## Filtering, dates, and limits

Both connectors accept `--config country=Ghana` (or another country name). Matching is exact and
case-insensitive, using the upstream's spelling. It excludes events with no country, including
online events without a country. The feed is still downloaded in full before filtering. A source
URL is unique in Event Bank, so it cannot be registered again just to change the filter; edit the
existing source's `config` in admin. Previously imported events are not removed when filters change.

Both sources generally supply dates without times or timezones. These become all-day records;
use `--timezone UTC` for a geographically mixed feed, or the appropriate local timezone for a
single-region source. Exact start times must be confirmed with the organiser. Dates are mapped
as supplied; no cancellation is inferred from a missing row. Past entries are retained as supplied.

The existing safety limits apply: 2 MiB per response and 500 collected events per run. A larger
feed is processed up to the first 500 events and the run records a `run_limit_reached` note;
the rest are not imported (missing events are never deleted). Prefer a topic file or country
filter if the limit is reached. A valid but empty result still produces the pipeline's existing
`no_events` failure; it does not prove a source is broken. Malformed JSON or a wrong overall shape
fails the run atomically. A single row with a malformed date is skipped as `invalid_date` and
listed in the run's skipped details; the other rows are imported.

## Attribution

Each event stores its credit in a separate `attribution` field (at most 500 characters), returned
by `/api/events/` and appended to Telegram alerts and digests. Descriptions are not used for it.

- AgentConfs: the creator, source link, CC BY 4.0 link, an indication of normalisation, and the
  source listing URL when provided.
- confs.tech: a short credit to the confs.tech contributors and a link to the repository's MIT
  licence (`LICENSE.md`). The full MIT permission and warranty notice is reproduced below rather
  than in every event and message.

The aggregator is not mislabelled as the event organiser. Future public pages, exports and
notification templates must keep the `attribution` text when reusing this data. Do not assume the
repository's software license replaces the datasets' licenses. No attendee records, images or
arbitrary upstream descriptions are copied by these connectors.

**Decision for the operator:** whether a short credit with a licence link satisfies the MIT
licence's requirement to include its notice "in all copies or substantial portions" of the
dataset is a legal judgement. If you need the full notice with every copy, publish it alongside
any bulk export.

### confs.tech licence notice

> Permission is hereby granted, free of charge, to any person obtaining a copy of this software
> and associated documentation files (the "Software"), to deal in the Software without
> restriction, including without limitation the rights to use, copy, modify, merge, publish,
> distribute, sublicense, and/or sell copies of the Software, and to permit persons to whom the
> Software is furnished to do so, subject to the following conditions:
>
> The above copyright notice and this permission notice shall be included in all copies or
> substantial portions of the Software.
>
> THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING
> BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
> NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM,
> DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
> OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.

The copyright line itself is in the upstream
[LICENSE.md](https://github.com/tech-conferences/conference-data/blob/main/LICENSE.md).

## Verification

`tests/test_open_datasets.py` uses hand-written, fictional rows shaped like the public datasets.
It covers URL matching, MIME handling through `SafeFetcher`, field mapping, country filtering,
tentative dates, source identity, persisted attribution, idempotence, edits and HTTP failures.
Normal tests block outbound sockets. Live smoke checks are separate and read-only.

On 2026-10-23, the confs.tech `2026/python.json` feed was fetched through the production
`SafeFetcher` and connector: 11 events collected, 11 normalised, zero skipped. No live records
were written to the application database.
