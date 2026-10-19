"""Connector contract. This package must not import Django so connectors stay easy to test."""

import enum
from abc import ABC, abstractmethod
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, ClassVar, Protocol
from urllib.parse import urlsplit, urlunsplit


class EventStatus(enum.StrEnum):
    SCHEDULED = "scheduled"
    CANCELLED = "cancelled"
    POSTPONED = "postponed"
    RESCHEDULED = "rescheduled"
    MOVED_ONLINE = "moved_online"


class AttendanceMode(enum.StrEnum):
    UNKNOWN = ""
    OFFLINE = "offline"
    ONLINE = "online"
    MIXED = "mixed"


@dataclass(frozen=True)
class RawEvent:
    """An event as read from a source, before the pipeline normalises it.

    `external_id` must be stable for the same event across fetches; it is the
    deduplication key within a source. Naive datetimes are interpreted in the
    event's `timezone`, falling back to the source's timezone. A `date` means
    an all-day event.
    """

    external_id: str
    title: str
    url: str
    starts_at: datetime | date | None = None
    ends_at: datetime | date | None = None
    timezone: str | None = None
    description: str | None = None
    venue_name: str | None = None
    address: str | None = None
    attendance_mode: AttendanceMode = AttendanceMode.UNKNOWN
    status: EventStatus = EventStatus.SCHEDULED
    organizer_name: str | None = None
    image_url: str | None = None
    extra: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SourceSpec:
    url: str
    timezone: str
    config: Mapping[str, Any] = field(default_factory=dict)
    credentials: Mapping[str, str] = field(default_factory=dict, repr=False)


@dataclass(frozen=True)
class FetchResponse:
    url: str
    status_code: int
    content_type: str
    text: str


class Fetcher(Protocol):
    def get(self, url: str, *, headers: Mapping[str, str] | None = None) -> FetchResponse: ...


class ConnectorError(Exception):
    """The connector could not use the source: bad configuration, credentials or content."""

    code = "connector_error"

    def __init__(self, message: str, code: str | None = None):
        super().__init__(message)
        if code is not None:
            self.code = code


class Connector(ABC):
    """Base class for source connectors.

    Connectors never perform network I/O themselves: they receive a `Fetcher`
    that enforces the SSRF policy, restricted to `allowed_hosts`.
    """

    slug: ClassVar[str]
    name: ClassVar[str]
    allowed_hosts: ClassVar[frozenset[str]]
    """Hosts the fetcher may contact for this connector, including redirect targets."""

    url_hosts: ClassVar[frozenset[str] | None] = None
    """Hosts accepted in registered source URLs. Defaults to `allowed_hosts`."""

    @classmethod
    def source_url_hosts(cls) -> frozenset[str]:
        return cls.url_hosts if cls.url_hosts is not None else cls.allowed_hosts

    @classmethod
    def matches(cls, url: str) -> bool:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        return (
            parts.scheme == "https"
            and host in cls.source_url_hosts()
            and cls.matches_path(parts.path)
        )

    @classmethod
    def matches_path(cls, path: str) -> bool:
        return True

    @classmethod
    def validate_config(cls, config: Mapping[str, Any]) -> dict[str, Any]:
        """Return the cleaned per-source config, or raise ConnectorError(code="invalid_config")."""
        if config:
            raise ConnectorError(f"{cls.name} takes no configuration", code="invalid_config")
        return {}

    @classmethod
    def credentials_from_env(
        cls, config: Mapping[str, Any], environ: Mapping[str, str]
    ) -> dict[str, str]:
        """Look up secrets for a source. Secrets live in the environment, never in the database."""
        return {}

    @classmethod
    def canonicalize_url(cls, url: str) -> str:
        parts = urlsplit(url.strip())
        host = (parts.hostname or "").lower()
        netloc = host if parts.port in (None, 443) else f"{host}:{parts.port}"
        return urlunsplit(("https", netloc, parts.path or "/", parts.query, ""))

    @abstractmethod
    def collect(self, source: SourceSpec, fetcher: Fetcher) -> Iterable[RawEvent]: ...
