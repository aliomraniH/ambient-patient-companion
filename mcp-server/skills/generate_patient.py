"""Skill: generate_patient — create or import a patient record."""

from __future__ import annotations

import json
import logging
import sys
import uuid
from datetime import datetime

from fastmcp import FastMCP

from db.connection import get_pool
from skills.base import log_skill_execution

logging.basicConfig(level=logging.INFO, stream=sys.stderr)
logger = logging.getLogger(__name__)


def register(mcp: FastMCP):
    @mcp.tool
    async def generate_patient(
        first_name: str,
        last_name: str,
        birth_date: str,
        gender: str,
        mrn: str = "",
        race: str = "",
        ethnicity: str = "",
        address_line: str = "",
        city: str = "",
        state: str = "",
        zip_code: str = "",
        conditions_json: str = "[]",
        medications_json: str = "[]",
        is_synthetic: bool = True,
    ) -> str:
        """Create a patient record in the database.

        Args:
            first_name: Patient's first name
            last_name: Patient's last name
            birth_date: Birth date in YYYY-MM-DD format
            gender: Patient gender (male/female/other)
            mrn: Medical record number (auto-generated if empty)
            race: Patient race
            ethnicity: Patient ethnicity
            address_line: Street address
            city: City
            state: State
            zip_code: ZIP code
            conditions_json: JSON array of condition objects
            medications_json: JSON array of medication objects
            is_synthetic: Whether this is synthetic data
        """
        pool = await get_pool()
        patient_id = str(uuid.uuid4())
        if not mrn:
            mrn = f"SYN-{uuid.uuid4().hex[:8].upper()}"

        try:
            conditions = json.loads(conditions_json)
            medications = json.loads(medications_json)
        except json.JSONDecodeError as e:
            return f"Error: Invalid JSON — {e}"

        try:
            async with pool.acquire() as conn:
                # Insert patient
                await conn.execute(
                    """
                    INSERT INTO patients
                        (id, mrn, first_name, last_name, birth_date, gender,
                         race, ethnicity, address_line, city, state, zip_code,
                         is_synthetic, created_at)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14)
                    ON CONFLICT (mrn) DO NOTHING
                    """,
                    patient_id, mrn, first_name, last_name, birth_date, gender,
                    race, ethnicity, address_line, city, state, zip_code,
                    is_synthetic, datetime.utcnow(),
                )

                # Insert conditions
                cond_count = 0
                for cond in conditions:
                    await conn.execute(
                        """
                        INSERT INTO patient_conditions
                            (id, patient_id, code, display, system, onset_date,
                             clinical_status)
                        VALUES ($1,$2,$3,$4,$5,$6,$7)
                        ON CONFLICT DO NOTHING
                        """,
                        str(uuid.uuid4()), patient_id,
                        cond.get("code", ""), cond.get("display", ""),
                        cond.get("system", ""), cond.get("onset_date"),
                        cond.get("clinical_status", "active"),
                    )
                    cond_count += 1

                # Insert medications
                med_count = 0
                for med in medications:
                    await conn.execute(
                        """
                        INSERT INTO patient_medications
                            (id, patient_id, code, display, system, status,
                             authored_on)
                        VALUES ($1,$2,$3,$4,$5,$6,$7)
                        ON CONFLICT DO NOTHING
                        """,
                        str(uuid.uuid4()), patient_id,
                        med.get("code", ""), med.get("display", ""),
                        med.get("system", ""), med.get("status", "active"),
                        med.get("authored_on"),
                    )
                    med_count += 1

                await log_skill_execution(
                    conn, "generate_patient", patient_id, "completed",
                    output_data={
                        "mrn": mrn,
                        "conditions": cond_count,
                        "medications": med_count,
                    },
                )

            return (
                f"✓ Created patient {first_name} {last_name} "
                f"(id={patient_id}, mrn={mrn}, "
                f"{cond_count} conditions, {med_count} medications)"
            )

        except Exception as e:
            logger.error("generate_patient failed: %s", e)
            try:
                async with pool.acquire() as conn:
                    await log_skill_execution(
                        conn, "generate_patient", patient_id, "failed",
                        error_message=str(e),
                    )
            except Exception:
                logger.error("Failed to log skill execution error")
            return f"Error: {e}"
