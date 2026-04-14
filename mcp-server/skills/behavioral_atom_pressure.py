"""Atom pressure score utilities.

The `atom_pressure_scores` materialized view is created by migration 010.
This module exposes helpers to refresh and read it.
"""
from __future__ import annotations

import logging
import sys

logging.basicConfig(level=logging.INFO, stream=sys.stderr)
logger = logging.getLogger(__name__)

REFRESH_SQL_CONCURRENT = "REFRESH MATERIALIZED VIEW CONCURRENTLY atom_pressure_scores"
REFRESH_SQL_PLAIN = "REFRESH MATERIALIZED VIEW atom_pressure_scores"


async def refresh_pressure_scores(conn) -> None:
    """Refresh the atom_pressure_scores materialized view.

    CONCURRENTLY requires a unique index (idx_aps_patient in migration 010)
    AND at least one row in the base table. Falls back to a plain refresh
    if concurrent refresh is not possible (e.g. first refresh on empty view).
    """
    try:
        await conn.execute(REFRESH_SQL_CONCURRENT)
    except Exception as e:
        logger.info("Concurrent refresh failed (%s); retrying plain refresh",
                    type(e).__name__)
        try:
            await conn.execute(REFRESH_SQL_PLAIN)
        except Exception as e2:
            logger.warning("Pressure-score refresh failed: %s", type(e2).__name__)


async def get_pressure_score(conn, patient_id: str) -> dict:
    row = await conn.fetchrow(
        "SELECT * FROM atom_pressure_scores WHERE patient_id = $1::uuid",
        patient_id,
    )
    if not row:
        return {
            "pressure_score": 0.0,
            "present_atom_count": 0,
            "total_atom_count": 0,
            "latest_atom_date": None,
            "earliest_atom_date": None,
        }
    return dict(row)


def register(mcp):  # pragma: no cover - no-op to silence skill loader warning
    return
