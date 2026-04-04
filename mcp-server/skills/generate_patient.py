"""Skill: generate_patient — import a patient from Synthea FHIR Bundle or manual input."""

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
        synthea_file: str = "",
        first_name: str = "",
        last_name: str = "",
        birth_date: str = "",
        gender: str = "",
        mrn: str = "",
        race: str = "",
        ethnicity: str = "",
        address_line: str = "",
        city: str = "",
        state: str = "",
        zip_code: str = "",
        insurance_type: str = "",
        conditions_json: str = "[]",
        medications_json: str = "[]",
        is_synthetic: bool = True,
    ) -> str:
        """Import a patient from a Synthea FHIR JSON file or create manually.

        Args:
            synthea_file: Path to Synthea FHIR Bundle JSON file (if provided, other fields are extracted from it)
            first_name: Patient's first name (used if synthea_file is empty)
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
            insurance_type: Insurance type
            conditions_json: JSON array of condition objects
            medications_json: JSON array of medication objects
            is_synthetic: Whether this is synthetic data
        """
        pool = await get_pool()
        patient_id = str(uuid.uuid4())

        try:
            conditions = []
            medications = []

            if synthea_file:
                # Load from Synthea FHIR Bundle
                from adapters.synthea import SyntheaAdapter
                adapter = SyntheaAdapter()
                with open(synthea_file, "r") as f:
                    bundle = json.load(f)

                from transforms.fhir_to_schema import (
                    transform_patient,
                    transform_conditions,
                    transform_medications,
                )

                # Extract resources
                patient_resources = [
                    entry["resource"]
                    for entry in bundle.get("entry", [])
                    if entry.get("resource", {}).get("resourceType") == "Patient"
                ]
                if not patient_resources:
                    return "Error: No Patient resource found in FHIR Bundle"

                condition_resources = [
                    entry["resource"]
                    for entry in bundle.get("entry", [])
                    if entry.get("resource", {}).get("resourceType") == "Condition"
                ]
                medication_resources = [
                    entry["resource"]
                    for entry in bundle.get("entry", [])
                    if entry.get("resource", {}).get("resourceType") == "MedicationRequest"
                ]

                pt = transform_patient(patient_resources[0])
                patient_id = pt["id"]
                first_name = pt["first_name"]
                last_name = pt["last_name"]
                birth_date = str(pt["birth_date"]) if pt["birth_date"] else ""
                gender = pt["gender"]
                mrn = pt["mrn"]
                race = pt["race"]
                ethnicity = pt["ethnicity"]
                address_line = pt["address_line"]
                city = pt["city"]
                state = pt["state"]
                zip_code = pt["zip_code"]

                conditions = transform_conditions(condition_resources, patient_id)
                medications = transform_medications(medication_resources, patient_id)

            else:
                # Manual creation
                try:
                    conditions = json.loads(conditions_json)
                    medications = json.loads(medications_json)
                except json.JSONDecodeError as e:
                    return f"Error: Invalid JSON — {e}"

            if not mrn:
                mrn = f"SYN-{uuid.uuid4().hex[:8].upper()}"

            async with pool.acquire() as conn:
                # Insert patient
                await conn.execute(
                    """
                    INSERT INTO patients
                        (id, mrn, first_name, last_name, birth_date, gender,
                         race, ethnicity, address_line, city, state, zip_code,
                         insurance_type, is_synthetic, created_at, data_source)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16)
                    ON CONFLICT (mrn) DO UPDATE SET
                        first_name = EXCLUDED.first_name,
                        last_name = EXCLUDED.last_name,
                        birth_date = EXCLUDED.birth_date,
                        gender = EXCLUDED.gender,
                        race = EXCLUDED.race,
                        ethnicity = EXCLUDED.ethnicity,
                        address_line = EXCLUDED.address_line,
                        city = EXCLUDED.city,
                        state = EXCLUDED.state,
                        zip_code = EXCLUDED.zip_code,
                        insurance_type = EXCLUDED.insurance_type
                    """,
                    patient_id, mrn, first_name, last_name, birth_date or None,
                    gender, race, ethnicity, address_line, city, state, zip_code,
                    insurance_type, is_synthetic, datetime.utcnow(), "synthea",
                )

                # Get the actual patient_id (in case of ON CONFLICT)
                row = await conn.fetchrow(
                    "SELECT id FROM patients WHERE mrn = $1", mrn
                )
                if row:
                    patient_id = str(row["id"])

                # Insert conditions
                cond_count = 0
                for cond in conditions:
                    await conn.execute(
                        """
                        INSERT INTO patient_conditions
                            (id, patient_id, code, display, system, onset_date,
                             clinical_status, data_source)
                        VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
                        ON CONFLICT DO NOTHING
                        """,
                        cond.get("id", str(uuid.uuid4())), patient_id,
                        cond.get("code", ""), cond.get("display", ""),
                        cond.get("system", ""), cond.get("onset_date"),
                        cond.get("clinical_status", "active"), "synthea",
                    )
                    cond_count += 1

                # Insert medications
                med_count = 0
                for med in medications:
                    await conn.execute(
                        """
                        INSERT INTO patient_medications
                            (id, patient_id, code, display, system, status,
                             authored_on, data_source)
                        VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
                        ON CONFLICT DO NOTHING
                        """,
                        med.get("id", str(uuid.uuid4())), patient_id,
                        med.get("code", ""), med.get("display", ""),
                        med.get("system", ""), med.get("status", "active"),
                        med.get("authored_on"), "synthea",
                    )
                    med_count += 1

                # Insert data_sources record
                await conn.execute(
                    """
                    INSERT INTO data_sources
                        (id, patient_id, source_name, is_active, connected_at, data_source)
                    VALUES ($1, $2, $3, $4, $5, $6)
                    ON CONFLICT (patient_id, source_name) DO NOTHING
                    """,
                    str(uuid.uuid4()), patient_id, "synthea", True,
                    datetime.utcnow(), "synthea",
                )

                await log_skill_execution(
                    conn, "generate_patient", patient_id, "completed",
                    output_data={
                        "mrn": mrn,
                        "conditions": cond_count,
                        "medications": med_count,
                    },
                )

            return (
                f"OK Imported {first_name} {last_name} | "
                f"{cond_count} conditions | {med_count} meds | {patient_id}"
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
