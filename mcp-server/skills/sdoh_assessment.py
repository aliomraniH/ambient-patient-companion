"""Skill: run_sdoh_assessment — assess and store SDoH flags."""

from __future__ import annotations

import json
import logging
import sys
import uuid
from datetime import date, datetime

from fastmcp import FastMCP

from db.connection import get_pool
from generators.sdoh_profile import generate_sdoh_flags
from skills.base import log_skill_execution

logging.basicConfig(level=logging.INFO, stream=sys.stderr)
logger = logging.getLogger(__name__)


def register(mcp: FastMCP):
    @mcp.tool
    async def run_sdoh_assessment(
        patient_id: str,
        screening_date: str = "",
    ) -> str:
        """Run a Social Determinants of Health assessment for a patient.

        Args:
            patient_id: UUID of the patient
            screening_date: Date in YYYY-MM-DD format (defaults to today)
        """
        pool = await get_pool()
        try:
            if screening_date:
                s_date = date.fromisoformat(screening_date)
            else:
                s_date = date.today()

            flags = generate_sdoh_flags(patient_id, screening_date=s_date)

            flags_inserted = 0
            interventions_inserted = 0

            async with pool.acquire() as conn:
                for flag in flags:
                    result = await conn.execute(
                        """
                        INSERT INTO patient_sdoh_flags
                            (id, patient_id, domain, severity, screening_date,
                             notes, data_source)
                        VALUES (gen_random_uuid(), $1, $2, $3, $4, $5, $6)
                        ON CONFLICT (patient_id, domain) DO UPDATE SET
                            severity = EXCLUDED.severity,
                            screening_date = EXCLUDED.screening_date,
                            notes = EXCLUDED.notes
                        """,
                        flag["patient_id"], flag["domain"],
                        flag["severity"], flag["screening_date"],
                        flag["notes"], "synthea",
                    )
                    flags_inserted += 1

                    # Create agent intervention for moderate/high severity
                    if flag["severity"] in ("moderate", "high"):
                        await conn.execute(
                            """
                            INSERT INTO agent_interventions
                                (id, patient_id, intervention_type, summary,
                                 delivered_at, source_skill, data_source)
                            VALUES (gen_random_uuid(), $1, 'alert', $2, $3,
                                    'sdoh_assessment', $4)
                            """,
                            patient_id,
                            f"SDoH flag: {flag['domain']} severity={flag['severity']}",
                            datetime.utcnow(), "synthea",
                        )
                        interventions_inserted += 1

                await log_skill_execution(
                    conn, "run_sdoh_assessment", patient_id, "completed",
                    output_data={
                        "date": str(s_date),
                        "flags": flags_inserted,
                        "interventions": interventions_inserted,
                    },
                )

            return (
                f"✓ SDoH assessment for patient {patient_id}: "
                f"{flags_inserted} flags, {interventions_inserted} interventions"
            )

        except Exception as e:
            logger.error("run_sdoh_assessment failed: %s", e)
            try:
                async with pool.acquire() as conn:
                    await log_skill_execution(
                        conn, "run_sdoh_assessment", patient_id, "failed",
                        error_message=str(e),
                    )
            except Exception:
                logger.error("Failed to log skill execution error")
            return f"Error: {e}"
