"""Read confs.tech's MIT-licensed, per-year/topic conference data.

The upstream has no event IDs. URL + start date identifies an occurrence,
including repeated conferences at the same URL in the same year. A URL or
start-date change therefore creates a new event; it cannot be matched safely.
"""

import hashlib
import json
import re
from urllib.parse import urlsplit, urlunsplit

from connectors.base import AttendanceMode, Connector, ConnectorError, RawEvent
from connectors.extract.event_json import (
    country_config,
    date_field,
    event_rows,
    location,
    matches_country,
    text_field,
)
from connectors.registry import register

_PATH = re.compile(
    r"/tech-conferences/conference-data/(?:main|refs/heads/main)/"
    r"conferences/(20\d{2})/([a-z0-9]+(?:-[a-z0-9]+)*)\.json"
)
DATA_LICENSE = (
    "Permission is hereby granted, free of charge, to any person obtaining a copy of this "
    'software and associated documentation files (the "Software"), to deal in the Software '
    "without restriction, including without limitation the rights to use, copy, modify, merge, "
    "publish, distribute, sublicense, and/or sell copies of the Software, and to permit persons "
    "to whom the Software is furnished to do so, subject to the following conditions:\n\n"
    "The above copyright notice and this permission notice shall be included in all copies or "
    "substantial portions of the Software.\n\n"
    'THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, '
    "INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR "
    "PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE "
    "FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR "
    "OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER "
    "DEALINGS IN THE SOFTWARE."
)
# Short per-event credit. The full notice above is published with the project documentation
# (docs/free-event-sources.md) and linked here rather than repeated in every event and alert.
LICENSE_URL = "https://github.com/tech-conferences/conference-data/blob/main/LICENSE.md"
ATTRIBUTION = (
    "Event data: confs.tech contributors (https://github.com/tech-conferences/conference-data), "
    f"MIT license: {LICENSE_URL}. Reformatted by Event Bank."
)


@register
class ConfsTechConnector(Connector):
    slug = "confstech"
    name = "confs.tech (public JSON)"
    allowed_hosts = frozenset({"raw.githubusercontent.com"})
    accepted_content_types = frozenset({"application/json", "text/plain"})

    @classmethod
    def matches_path(cls, path):
        return _PATH.fullmatch(path) is not None

    @classmethod
    def canonicalize_url(cls, url):
        parts = urlsplit(url)
        path = parts.path.replace("/refs/heads/main/", "/main/")
        return urlunsplit(("https", "raw.githubusercontent.com", path, "", ""))

    @classmethod
    def validate_config(cls, config):
        return country_config(config)

    def collect(self, source, fetcher):
        config = self.validate_config(source.config)
        response = fetcher.get(self.canonicalize_url(source.url))
        for row in event_rows(response.text):
            if not matches_country(row, config):
                continue
            url = text_field(row.get("url"))
            try:
                start = date_field(row.get("startDate"))
                end = date_field(row.get("endDate"))
            except ConnectorError:
                # One malformed row must not fail the whole file.
                yield RawEvent.skipped(url or text_field(row.get("name")), "invalid_date")
                continue
            # Missing identity components must not collapse unrelated entries.
            identity = json.dumps([url, start.isoformat() if start else None])
            external_id = hashlib.sha256(identity.encode()).hexdigest() if url and start else ""
            online = row.get("online")
            yield RawEvent(
                external_id=external_id,
                title=text_field(row.get("name")),
                url=url,
                starts_at=start,
                ends_at=end,
                attribution=ATTRIBUTION,
                address=location(row),
                attendance_mode=(
                    AttendanceMode.ONLINE
                    if online is True
                    else AttendanceMode.OFFLINE
                    if online is False
                    else AttendanceMode.UNKNOWN
                ),
            )
