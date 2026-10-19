import hashlib
import json
from collections.abc import Mapping
from typing import Any

from pipeline.normalize import TRACKED_FIELDS


def fingerprint(snapshot: Mapping[str, Any]) -> str:
    """Stable SHA-256 over the tracked fields of a snapshot."""
    payload = json.dumps(
        {name: snapshot.get(name) for name in TRACKED_FIELDS},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
