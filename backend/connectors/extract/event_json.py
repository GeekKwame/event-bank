"""Small helpers for published event datasets (no network access)."""

import json
from collections.abc import Mapping
from datetime import date
from typing import Any

from connectors.base import ConnectorError


def event_rows(text: str, *, envelope: str | None = None) -> list[dict[str, Any]]:
    try:
        data = json.loads(text)
    except ValueError:
        raise ConnectorError("Invalid event JSON", code="unexpected_response") from None
    if envelope and isinstance(data, Mapping):
        data = data.get(envelope)
    if not isinstance(data, list) or any(not isinstance(row, dict) for row in data):
        raise ConnectorError("Expected a list of event objects", code="unexpected_response")
    return data


def text_field(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def date_field(value: Any) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, str):
        try:
            # These datasets publish calendar dates, not precise event times.
            if len(value) == 10:
                return date.fromisoformat(value)
        except ValueError:
            pass
    raise ConnectorError("Invalid event date; expected YYYY-MM-DD", code="unexpected_response")


def location(row: Mapping[str, Any]) -> str:
    return ", ".join(value for key in ("city", "country") if (value := text_field(row.get(key))))


def country_config(config: Mapping[str, Any]) -> dict[str, str]:
    if set(config) - {"country"}:
        raise ConnectorError("Only country is supported", code="invalid_config")
    if "country" not in config:
        return {}
    country = text_field(config["country"])
    if not country or len(country) > 100:
        raise ConnectorError("country must be a nonempty country name", code="invalid_config")
    return {"country": country}


def matches_country(row: Mapping[str, Any], config: Mapping[str, str]) -> bool:
    country = config.get("country")
    return country is None or text_field(row.get("country")).casefold() == country.casefold()
