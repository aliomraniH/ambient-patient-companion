"""Shared skill helpers and base class."""

from __future__ import annotations

import logging
import sys
import json
from datetime import datetime, date

logging.basicConfig(level=logging.INFO, stream=sys.stderr)
logger = logging.getLogger(__name__)


async def log_skill_execution(
    conn,
    skill_name: str,
    patient_id: str | None,
    status: str,
    output_data: dict | None = None,
    error_message: str | None = None,
) -> None:
    """Insert a row into skill_executions for audit trail."""
    await conn.execute(
        """
        INSERT INTO skill_executions
            (skill_name, patient_id, status, output_data, error_message, execution_date)
        VALUES ($1, $2, $3, $4, $5, $6)
        """,
        skill_name,
        patient_id,
        status,
        json.dumps(output_data) if output_data else None,
        error_message,
        datetime.utcnow(),
    )
