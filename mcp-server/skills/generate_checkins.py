"""Skill: generate_daily_checkins — produce and store check-in + adherence data."""

from __future__ import annotations

import logging
import sys
from datetime import date, datetime

from fastmcp import FastMCP

from db.connection import get_pool
from generators.behavioral_model import generate_checkins, generate_adherence_records
from skills.base import log_skill_execution

logging.basicConfig(level=logging.INFO, stream=sys.stderr)
logger = logging.getLogger(__name__)


def register(mcp: FastMCP):
    @mcp.tool
    async def generate_daily_checkins(
        patient_id: str,
        target_date: str = "",
    ) -> str:
        """Generate a daily check-in and medication adherence records.

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

            seed = int(day.toordinal())
            checkins = generate_checkins(patient_id, day, day, seed)

            checkin_inserted = 0
            adherence_inserted = 0

            async with pool.acquire() as conn:
                # Insert check-in
                for ci in checkins:
                    result = await conn.execute(
                        """
                        INSERT INTO daily_checkins
                            (id, patient_id, checkin_date, mood, mood_numeric,
                             energy, stress_level, sleep_hours, notes)
                        VALUES (gen_random_uuid(), $1,$2,$3,$4,$5,$6,$7,$8)
                        ON CONFLICT (patient_id, checkin_date) DO NOTHING
                        """,
                        ci["patient_id"], ci["checkin_date"], ci["mood"],
                        ci["mood_numeric"], ci["energy"], ci["stress_level"],
                        ci["sleep_hours"], ci["notes"],
                    )
                    if "INSERT" in result:
                        checkin_inserted += 1

                # Get patient medications for adherence records
                med_rows = await conn.fetch(
                    "SELECT id FROM patient_medications WHERE patient_id = $1",
                    patient_id,
                )
                med_ids = [str(row["id"]) for row in med_rows]

                if med_ids:
                    adherence_recs = generate_adherence_records(
                        patient_id, med_ids, day, day, seed,
                    )
                    for ar in adherence_recs:
                        result = await conn.execute(
                            """
                            INSERT INTO medication_adherence
                                (id, patient_id, medication_id, adherence_date,
                                 taken, notes)
                            VALUES (gen_random_uuid(), $1,$2,$3,$4,$5)
                            ON CONFLICT (patient_id, medication_id, adherence_date)
                                DO NOTHING
                            """,
                            ar["patient_id"], ar["medication_id"],
                            ar["adherence_date"], ar["taken"], ar["notes"],
                        )
                        if "INSERT" in result:
                            adherence_inserted += 1

                await log_skill_execution(
                    conn, "generate_daily_checkins", patient_id, "completed",
                    output_data={
                        "date": str(day),
                        "checkins": checkin_inserted,
                        "adherence_records": adherence_inserted,
                    },
                )

            return (
                f"✓ Generated {checkin_inserted} check-in(s) and "
                f"{adherence_inserted} adherence records "
                f"for patient {patient_id} on {day}"
            )

        except Exception as e:
            logger.error("generate_daily_checkins failed: %s", e)
            try:
                async with pool.acquire() as conn:
                    await log_skill_execution(
                        conn, "generate_daily_checkins", patient_id, "failed",
                        error_message=str(e),
                    )
            except Exception:
                logger.error("Failed to log skill execution error")
            return f"Error: {e}"
