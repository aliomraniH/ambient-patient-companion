"""Manual entry adapter: handles direct patient check-in submissions.

Writes directly to the database without FHIR parsing.
"""

from __future__ import annotations

import logging
import sys
import uuid
from datetime import datetime
from typing import Any

logging.basicConfig(level=logging.INFO, stream=sys.stderr)
logger = logging.getLogger(__name__)


class ManualEntryAdapter:
    """Adapter for patient-reported data entered via check-in forms."""

    source_track: str = "manual"
    source_name: str = "manual"

    async def write_checkin(
        self,
        patient_id: str,
        payload: dict[str, Any],
        pool,
    ) -> str:
        """Write a patient check-in directly to the database.

        Args:
            patient_id: UUID of the patient
            payload: Dict with keys: mood, mood_numeric, energy, stress_level,
                     sleep_hours, notes (all optional except mood)
            pool: asyncpg connection pool

        Returns:
            The checkin record ID as a string.
        """
        checkin_id = str(uuid.uuid4())
        try:
            async with pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO daily_checkins
                        (id, patient_id, checkin_date, mood, mood_numeric,
                         energy, stress_level, sleep_hours, notes, data_source)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                    ON CONFLICT (patient_id, checkin_date) DO UPDATE SET
                        mood = EXCLUDED.mood,
                        mood_numeric = EXCLUDED.mood_numeric,
                        energy = EXCLUDED.energy,
                        stress_level = EXCLUDED.stress_level,
                        sleep_hours = EXCLUDED.sleep_hours,
                        notes = EXCLUDED.notes,
                        data_source = EXCLUDED.data_source
                    """,
                    checkin_id,
                    patient_id,
                    payload.get("checkin_date", datetime.utcnow().date()),
                    payload.get("mood", "okay"),
                    payload.get("mood_numeric", 3),
                    payload.get("energy", "moderate"),
                    payload.get("stress_level", 5),
                    payload.get("sleep_hours", 7.0),
                    payload.get("notes"),
                    "manual",
                )
            logger.info("Manual check-in written for patient %s", patient_id)
            return checkin_id

        except Exception as e:
            logger.error("Manual check-in failed for patient %s: %s", patient_id, e)
            raise
