"""Skill: generate_daily_vitals — produce and store vital-sign readings."""

from __future__ import annotations

import logging
import sys
from datetime import date, datetime

from fastmcp import FastMCP

from db.connection import get_pool
from generators.vitals_timeseries import (
    generate_bp_readings,
    generate_glucose_readings,
    generate_hrv_readings,
    generate_spo2_readings,
    generate_steps_readings,
    generate_weight_readings,
)
from skills.base import log_skill_execution

logging.basicConfig(level=logging.INFO, stream=sys.stderr)
logger = logging.getLogger(__name__)


def register(mcp: FastMCP):
    @mcp.tool
    async def generate_daily_vitals(
        patient_id: str,
        target_date: str = "",
    ) -> str:
        """Generate daily vital-sign readings for a patient.

        Args:
            patient_id: UUID of the patient
            target_date: Date in YYYY-MM-DD format (defaults to today)
        """
        pool = await get_pool()
        try:
            if target_date:
                day = date.fromisoformat(target_date)
            else:
                day = date.today()

            # Generate all vital types for the day
            seed = int(day.toordinal())
            all_readings: list[dict] = []
            all_readings.extend(generate_bp_readings(patient_id, day, day, seed))
            all_readings.extend(generate_glucose_readings(patient_id, day, day, seed))
            all_readings.extend(generate_hrv_readings(patient_id, day, day, seed))
            all_readings.extend(generate_spo2_readings(patient_id, day, day, seed))
            all_readings.extend(generate_steps_readings(patient_id, day, day, seed))
            all_readings.extend(generate_weight_readings(patient_id, day, day, seed))

            inserted = 0
            async with pool.acquire() as conn:
                for r in all_readings:
                    is_abnormal = False
                    if r["metric_type"] == "bp_systolic" and r["value"] > 160:
                        is_abnormal = True
                    elif r["metric_type"] == "glucose_fasting" and r["value"] > 250:
                        is_abnormal = True

                    result = await conn.execute(
                        """
                        INSERT INTO biometric_readings
                            (id, patient_id, metric_type, value, unit,
                             measured_at, is_abnormal, day_of_month, data_source)
                        VALUES (gen_random_uuid(), $1, $2, $3, $4, $5, $6, $7, $8)
                        ON CONFLICT DO NOTHING
                        """,
                        r["patient_id"], r["metric_type"],
                        r["value"], r["unit"], r["measured_at"],
                        is_abnormal, day.day, "synthea",
                    )
                    if "INSERT" in result:
                        inserted += 1

                await log_skill_execution(
                    conn, "generate_daily_vitals", patient_id, "completed",
                    output_data={"date": str(day), "readings_inserted": inserted},
                )

            return (
                f"OK Generated {inserted} vital readings "
                f"for patient {patient_id} on {day}"
            )

        except Exception as e:
            logger.error("generate_daily_vitals failed: %s", e)
            try:
                async with pool.acquire() as conn:
                    await log_skill_execution(
                        conn, "generate_daily_vitals", patient_id, "failed",
                        error_message=str(e),
                    )
            except Exception:
                logger.error("Failed to log skill execution error")
            return f"Error: {e}"
