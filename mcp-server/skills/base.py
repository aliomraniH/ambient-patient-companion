"""Shared skill helpers and base class."""

from __future__ import annotations

import logging
import os
import sys
import json
from datetime import datetime, date

logging.basicConfig(level=logging.INFO, stream=sys.stderr)
logger = logging.getLogger(__name__)


async def get_data_track(conn) -> str:
    """Read active DATA_TRACK from system_config, falling back to env var."""
    try:
        row = await conn.fetchrow(
            "SELECT value FROM system_config WHERE key = 'DATA_TRACK'"
        )
        if row:
            return row["value"]
    except Exception:
        pass
    return os.environ.get("DATA_TRACK", "synthea")


async def log_skill_execution(
    conn,
    skill_name: str,
    patient_id: str | None,
    status: str,
    output_data: dict | None = None,
    error_message: str | None = None,
    data_source: str = "synthea",
) -> None:
    """Insert a row into skill_executions for audit trail."""
    await conn.execute(
        """
        INSERT INTO skill_executions
            (skill_name, patient_id, status, output_data, error_message,
             execution_date, data_source)
        VALUES ($1, $2, $3, $4, $5, $6, $7)
        """,
        skill_name,
        patient_id,
        status,
        json.dumps(output_data) if output_data else None,
        error_message,
        datetime.utcnow(),
        data_source,
    )
