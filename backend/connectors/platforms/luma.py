"""Luma calendars through Luma's official API (https://docs.luma.com/reference).

Luma's Terms of Use only allow access through its publicly supported interfaces,
so this connector never scrapes luma.com pages. It needs a calendar API key
(a Luma Plus feature), which Luma scopes to one calendar and which grants full
access to that calendar: the key is read from the environment and only ever
sent to public-api.luma.com.

The registered source URL (https://luma.com/<calendar-slug>) identifies the
calendar; every run checks that the API key belongs to that calendar.

Only events the calendar itself manages are collected. Events listed on the
calendar but managed by other hosts (`access=view`) are excluded until Luma
confirms in writing that redistributing them is permitted.

Field mapping follows Luma's published OpenAPI schema for
`GET /v1/calendars/get` and `GET /v1/calendars/events/list`.
"""

import json
import re
from collections.abc import Callable, Iterator, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar
from urllib.parse import urlencode, urlsplit

from connectors.base import (
    AttendanceMode,
    Connector,
    ConnectorError,
    EventStatus,
    Fetcher,
    RawEvent,
    SourceSpec,
)
from connectors.registry import register

API_HOST = "public-api.luma.com"
API_BASE = f"https://{API_HOST}"
SITE_URL = "https://luma.com"
API_KEY_HEADER = "x-luma-api-key"
DEFAULT_API_KEY_ENV = "LUMA_API_KEY"

PAGE_SIZE = 50
MAX_PAGES = 10
LOOKBACK = timedelta(days=1)

_API_KEY_ENV_NAME = re.compile(r"LUMA_API_KEY(?:_[A-Z0-9]+)*")
_SLUG = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}")
_ONLINE_LOCATION_TYPES = frozenset({"discord", "meet", "twitch", "twitter", "youtube", "zoom"})


def _calendar_slug(url: str) -> str:
    return urlsplit(url).path.strip("/").lower()


def _parse_api_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


@register
class LumaCalendarConnector(Connector):
    slug = "luma"
    name = "Luma calendar (official API)"
    allowed_hosts = frozenset({API_HOST})
    url_hosts = frozenset({"luma.com", "www.luma.com", "lu.ma"})

    clock: ClassVar[Callable[[], datetime]] = staticmethod(lambda: datetime.now(UTC))

    @classmethod
    def matches_path(cls, path: str) -> bool:
        return _SLUG.fullmatch(path.strip("/")) is not None

    @classmethod
    def canonicalize_url(cls, url: str) -> str:
        return f"{SITE_URL}/{_calendar_slug(url)}"

    @classmethod
    def validate_config(cls, config: Mapping[str, Any]) -> dict[str, Any]:
        unknown = sorted(set(config) - {"api_key_env"})
        if unknown:
            raise ConnectorError(f"Unknown Luma config keys: {unknown}", code="invalid_config")
        cleaned: dict[str, Any] = {}
        if "api_key_env" in config:
            name = str(config["api_key_env"])
            if not _API_KEY_ENV_NAME.fullmatch(name):
                raise ConnectorError(
                    "api_key_env must be LUMA_API_KEY or start with LUMA_API_KEY_",
                    code="invalid_config",
                )
            cleaned["api_key_env"] = name
        return cleaned

    @classmethod
    def credentials_from_env(
        cls, config: Mapping[str, Any], environ: Mapping[str, str]
    ) -> dict[str, str]:
        name = cls.validate_config(config).get("api_key_env", DEFAULT_API_KEY_ENV)
        api_key = environ.get(name, "").strip()
        if not api_key:
            raise ConnectorError(
                f"Environment variable {name} is not set", code="missing_credentials"
            )
        return {"api_key": api_key}

    def collect(self, source: SourceSpec, fetcher: Fetcher) -> Iterator[RawEvent]:
        self.validate_config(source.config)
        api_key = source.credentials.get("api_key")
        if not api_key:
            raise ConnectorError("Luma API key is missing", code="missing_credentials")
        headers = {API_KEY_HEADER: api_key, "Accept": "application/json"}

        calendar = self._get_json(fetcher, f"{API_BASE}/v1/calendars/get", headers)
        expected = _calendar_slug(source.url)
        actual = str(calendar.get("slug") or "").lower()
        if actual != expected:
            raise ConnectorError(
                "The API key belongs to a different Luma calendar than the registered URL",
                code="calendar_mismatch",
            )
        # A personal calendar's name may be a person's name, so it is not stored as the organiser.
        organizer = (
            None if calendar.get("is_personal") is not False else _text(calendar.get("name"))
        )

        after = (self.clock() - LOOKBACK).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        params = [
            ("after", after),
            ("sort_column", "start_at"),
            ("sort_direction", "asc"),
            ("pagination_limit", str(PAGE_SIZE)),
            ("access", "manage"),
        ]

        cursor = None
        for _ in range(MAX_PAGES):
            query = params + ([("pagination_cursor", cursor)] if cursor else [])
            page = self._get_json(
                fetcher, f"{API_BASE}/v1/calendars/events/list?{urlencode(query)}", headers
            )
            entries = page.get("entries")
            if not isinstance(entries, list):
                raise ConnectorError(
                    "Luma response has no entries list", code="unexpected_response"
                )
            for entry in entries:
                if isinstance(entry, Mapping):
                    raw = self.to_raw_event(entry, organizer_name=organizer)
                    if raw is not None:
                        yield raw
            cursor = page.get("next_cursor")
            if not page.get("has_more") or not cursor:
                return

    @staticmethod
    def _get_json(fetcher: Fetcher, url: str, headers: Mapping[str, str]) -> Mapping[str, Any]:
        response = fetcher.get(url, headers=headers)
        try:
            data = json.loads(response.text)
        except ValueError:
            raise ConnectorError("Luma returned invalid JSON", code="unexpected_response") from None
        if not isinstance(data, Mapping):
            raise ConnectorError("Luma returned an unexpected payload", code="unexpected_response")
        return data

    @staticmethod
    def to_raw_event(
        entry: Mapping[str, Any], *, organizer_name: str | None = None
    ) -> RawEvent | None:
        """Map one Luma event, or return None for events that must not be published."""
        if (
            entry.get("platform", "luma") != "luma"
            or entry.get("visibility") != "public"
            or entry.get("access") != "manage"
        ):
            return None
        external_id = _text(entry.get("id"))
        if external_id is None:
            return None

        location_type = entry.get("location_type")
        if location_type == "offline":
            attendance = AttendanceMode.OFFLINE
        elif location_type in _ONLINE_LOCATION_TYPES:
            attendance = AttendanceMode.ONLINE
        else:
            attendance = AttendanceMode.UNKNOWN

        geo = entry.get("geo_address_json")
        geo = geo if isinstance(geo, Mapping) else {}
        if entry.get("location_visibility") == "public":
            venue_name = _text(geo.get("address"))
            address = _text(geo.get("full_address")) or _text(geo.get("city_state"))
        else:
            # The host shows the address only to approved guests: keep the city at most.
            venue_name = None
            address = _text(geo.get("city_state")) or _text(geo.get("city"))

        url = _text(entry.get("url")) or ""
        if not url.startswith("https://"):
            url = f"{SITE_URL}/{url.strip('/')}" if _SLUG.fullmatch(url.strip("/")) else ""

        return RawEvent(
            external_id=external_id,
            title=_text(entry.get("name")) or "",
            url=url,
            starts_at=_parse_api_datetime(entry.get("start_at")),
            ends_at=_parse_api_datetime(entry.get("end_at")),
            timezone=_text(entry.get("timezone")),
            venue_name=venue_name,
            address=address,
            attendance_mode=attendance,
            status=EventStatus.SCHEDULED,
            organizer_name=organizer_name,
            image_url=_text(entry.get("cover_url")),
        )
