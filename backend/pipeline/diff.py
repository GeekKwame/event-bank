from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from pipeline.normalize import TRACKED_FIELDS

# Changes attendees need to know about. Edits to title, description, links or
# images are recorded as revisions but are not significant.
SIGNIFICANT_FIELDS = frozenset(
    {"starts_at", "ends_at", "is_all_day", "venue_name", "address", "attendance_mode", "status"}
)


@dataclass(frozen=True)
class ChangeSet:
    changed_fields: tuple[str, ...]
    before: dict[str, Any]
    after: dict[str, Any]

    @property
    def has_changes(self) -> bool:
        return bool(self.changed_fields)

    @property
    def is_significant(self) -> bool:
        return any(name in SIGNIFICANT_FIELDS for name in self.changed_fields)


def diff_snapshots(before: Mapping[str, Any], after: Mapping[str, Any]) -> ChangeSet:
    changed = tuple(name for name in TRACKED_FIELDS if before.get(name) != after.get(name))
    return ChangeSet(
        changed_fields=changed,
        before={name: before.get(name) for name in changed},
        after={name: after.get(name) for name in changed},
    )
