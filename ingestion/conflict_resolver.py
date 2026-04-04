"""Multi-source conflict resolution for the ingestion pipeline.

Precedence order: patient-reported > device > healthex > synthea
"""

from __future__ import annotations

import logging
import sys
from typing import Any

logging.basicConfig(level=logging.INFO, stream=sys.stderr)
logger = logging.getLogger(__name__)

# Higher number = higher priority
SOURCE_PRIORITY: dict[str, int] = {
    "synthea": 1,
    "healthex": 2,
    "device": 3,
    "withings": 3,
    "apple_health": 3,
    "dexcom": 3,
    "manual": 4,
}


class ConflictResolver:
    """Resolve conflicts when the same data exists from multiple sources."""

    def __init__(self, policy: str = "patient_first"):
        self.policy = policy

    def resolve(
        self,
        records: list[dict[str, Any]],
        key_field: str = "patient_id",
        conflict_field: str = "data_source",
    ) -> list[dict[str, Any]]:
        """Resolve conflicts among records sharing the same key.

        Args:
            records: List of record dicts, each must have key_field and conflict_field.
            key_field: Field used to group records (e.g. patient_id + metric_type + date).
            conflict_field: Field indicating the data source.

        Returns:
            List of resolved records (one per unique key), choosing the highest-priority source.
        """
        if not records:
            return []

        # Group records by key
        groups: dict[str, list[dict[str, Any]]] = {}
        for record in records:
            key = str(record.get(key_field, ""))
            groups.setdefault(key, []).append(record)

        resolved: list[dict[str, Any]] = []
        conflicts_detected = 0

        for key, group in groups.items():
            if len(group) == 1:
                resolved.append(group[0])
                continue

            # Multiple records for same key — resolve by priority
            conflicts_detected += 1
            best = max(
                group,
                key=lambda r: SOURCE_PRIORITY.get(
                    r.get(conflict_field, "synthea"), 0
                ),
            )
            resolved.append(best)

        if conflicts_detected > 0:
            logger.info(
                "Resolved %d conflicts using policy=%s",
                conflicts_detected,
                self.policy,
            )

        return resolved

    @staticmethod
    def apply(records: list[dict[str, Any]], policy: str = "patient_first") -> list[dict[str, Any]]:
        """Canonical conflict resolution interface.

        Groups records by (_table, _conflict_key fields) and keeps the
        highest-priority source per group.  Called by pipeline.run() and
        ingest_from_healthex().

        Args:
            records: List of record dicts (may contain _table and _conflict_key metadata).
            policy: Resolution policy (currently only "patient_first").

        Returns:
            De-duplicated list with conflicts resolved by source priority.
        """
        if not records:
            return []

        grouped: dict[tuple, list[dict[str, Any]]] = {}
        for rec in records:
            conflict_keys = rec.get("_conflict_key", [])
            if conflict_keys:
                key = tuple(str(rec.get(k, "")) for k in conflict_keys)
            else:
                # No conflict key — treat as unique (no dedup)
                key = (str(id(rec)),)
            table = rec.get("_table", "unknown")
            group_key = (table,) + key
            grouped.setdefault(group_key, []).append(rec)

        resolved: list[dict[str, Any]] = []
        conflicts_detected = 0

        for group_key, group in grouped.items():
            if len(group) == 1:
                resolved.append(group[0])
                continue
            conflicts_detected += 1
            best = max(
                group,
                key=lambda r: SOURCE_PRIORITY.get(
                    r.get("data_source", "synthea"), 0
                ),
            )
            resolved.append(best)

        if conflicts_detected > 0:
            logger.info(
                "apply(): resolved %d conflicts using policy=%s",
                conflicts_detected,
                policy,
            )

        return resolved

    def get_priority(self, source_name: str) -> int:
        """Return the priority rank of a data source."""
        return SOURCE_PRIORITY.get(source_name, 0)
