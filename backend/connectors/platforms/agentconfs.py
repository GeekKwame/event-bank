"""AgentConfs' published CC BY 4.0 JSON feed; no API key required.

Reuse statement: https://agentconfs.com/about
Attribution is stored in the event description, so it survives normalisation
and persistence. Never copy arbitrary fields such as contact/attendee data.
"""

from urllib.parse import urlsplit

from connectors.base import AttendanceMode, Connector, RawEvent
from connectors.extract.event_json import (
    country_config,
    date_field,
    event_rows,
    location,
    matches_country,
    text_field,
)
from connectors.registry import register

FEED_URL = "https://agentconfs.com/data/events.json"
ATTRIBUTION = (
    "Event data: AgentConfs (https://agentconfs.com/), licensed under CC BY 4.0 "
    "(https://creativecommons.org/licenses/by/4.0/). "
    "Selected fields have been reformatted and normalised by Event Bank."
)


@register
class AgentConfsConnector(Connector):
    slug = "agentconfs"
    name = "AgentConfs (public JSON)"
    allowed_hosts = frozenset({"agentconfs.com"})
    accepted_content_types = frozenset({"application/json"})

    @classmethod
    def matches_path(cls, path):
        return path in ("", "/", "/data/events.json")

    @classmethod
    def canonicalize_url(cls, url):
        return FEED_URL

    @classmethod
    def validate_config(cls, config):
        return country_config(config)

    def collect(self, source, fetcher):
        config = self.validate_config(source.config)
        response = fetcher.get(FEED_URL)
        for row in event_rows(response.text, envelope="events"):
            if not matches_country(row, config):
                continue
            # Do not turn provisional dates into confirmed event times.
            tentative = row.get("dates_to_be_confirmed") is True
            page = text_field(row.get("page"))
            parts = urlsplit(page)
            credit = ATTRIBUTION
            if parts.scheme == "https" and parts.netloc == "agentconfs.com":
                credit += f" Listing: {page}"
            if tentative:
                credit += " Dates to be confirmed."
            yield RawEvent(
                external_id=text_field(row.get("id")),
                title=text_field(row.get("name")),
                url=text_field(row.get("official_url")) or page,
                starts_at=None if tentative else date_field(row.get("start_date")),
                ends_at=None if tentative else date_field(row.get("end_date")),
                description=credit,
                venue_name=text_field(row.get("venue")),
                address=location(row),
                attendance_mode={
                    "in-person": AttendanceMode.OFFLINE,
                    "online": AttendanceMode.ONLINE,
                    "hybrid": AttendanceMode.MIXED,
                }.get(row.get("format"), AttendanceMode.UNKNOWN),
                organizer_name=text_field(row.get("organizer")),
            )
