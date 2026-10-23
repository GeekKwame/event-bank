import os
from collections.abc import Mapping
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.conf import settings
from django.db import IntegrityError, transaction

from apps.sources.models import Source
from connectors.base import Connector, ConnectorError
from connectors.http import FetchPolicy, Resolver, UnsafeURLError, check_url, system_resolver
from connectors.http import validate_url as validate_fetch_target
from connectors.registry import UnsupportedSourceError, match_connector


class SourceRegistrationError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def fetch_policy_for(connector_cls: type[Connector], hosts: frozenset[str] | None = None):
    content_types = connector_cls.accepted_content_types
    return FetchPolicy(
        allowed_hosts=frozenset(hosts if hosts is not None else connector_cls.allowed_hosts),
        max_redirects=settings.CONNECTOR_MAX_REDIRECTS,
        max_response_bytes=settings.CONNECTOR_MAX_RESPONSE_BYTES,
        connect_timeout=settings.CONNECTOR_CONNECT_TIMEOUT_SECONDS,
        read_timeout=settings.CONNECTOR_READ_TIMEOUT_SECONDS,
        total_timeout=settings.CONNECTOR_TOTAL_TIMEOUT_SECONDS,
        user_agent=settings.CONNECTOR_USER_AGENT,
        **({"accepted_content_types": frozenset(content_types)} if content_types else {}),
    )


def register_source(
    url: str,
    *,
    name: str = "",
    timezone: str | None = None,
    config: Mapping[str, Any] | None = None,
    submitted_by=None,
    approve: bool = False,
    reviewer=None,
    resolver: Resolver = system_resolver,
    environ: Mapping[str, str] = os.environ,
) -> Source:
    """Validate a source URL and store it. Sources start pending unless `approve` is set."""
    url = url.strip()
    try:
        check_url(url)
        connector_cls = match_connector(url)
    except UnsafeURLError as exc:
        raise SourceRegistrationError(exc.code, str(exc)) from None
    except UnsupportedSourceError as exc:
        raise SourceRegistrationError("unsupported_source", str(exc)) from None

    canonical_url = connector_cls.canonicalize_url(url)
    try:
        validate_fetch_target(
            canonical_url,
            fetch_policy_for(connector_cls, connector_cls.source_url_hosts()),
            resolver=resolver,
        )
    except UnsafeURLError as exc:
        raise SourceRegistrationError(exc.code, str(exc)) from None

    try:
        cleaned_config = connector_cls.validate_config(config or {})
        connector_cls.credentials_from_env(cleaned_config, environ)
    except ConnectorError as exc:
        raise SourceRegistrationError(exc.code, str(exc)) from None

    tz_name = timezone or settings.TIME_ZONE
    try:
        ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError):
        raise SourceRegistrationError("invalid_timezone", f"Unknown timezone {tz_name!r}") from None

    try:
        with transaction.atomic():
            source = Source.objects.create(
                url=canonical_url,
                connector=connector_cls.slug,
                name=name,
                timezone=tz_name,
                config=cleaned_config,
                submitted_by=submitted_by,
            )
    except IntegrityError:
        raise SourceRegistrationError(
            "duplicate_source", "This source is already registered"
        ) from None

    if approve:
        source.review(Source.Status.APPROVED, reviewer=reviewer)
    return source
