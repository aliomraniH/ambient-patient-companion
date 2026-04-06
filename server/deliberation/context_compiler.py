"""
context_compiler.py — Phase 0: Compile patient context package.
Assembles EHR data, prior knowledge, and applicable guidelines
into a standardized PatientContextPackage for both analyst models.
"""
import json
from datetime import datetime, timedelta
from typing import Optional
from .schemas import PatientContextPackage


async def compile_patient_context(
    patient_id: str,
    db_pool,                  # asyncpg connection pool
    vector_store,             # pgvector client
    days_lookback: int = 365
) -> PatientContextPackage:
    """
    Assemble complete patient context for deliberation.

    Pulls from:
    - patients table (demographics, provider)
    - conditions, medications, labs, vitals tables
    - care_gaps table
    - patient_knowledge table (is_current=true only)
    - guidelines vector store (top-10 most relevant by patient conditions)
    - appointments table (upcoming)

    Returns PatientContextPackage validated by Pydantic.
    """
    async with db_pool.acquire() as conn:
        # 1. Demographics and provider
        patient = await conn.fetchrow(
            "SELECT * FROM patients WHERE mrn = $1", patient_id
        )

        # 2. Active conditions
        conditions = await conn.fetch(
            """SELECT code, display, onset_date
               FROM conditions
               WHERE patient_id = $1 AND is_active = true
               ORDER BY onset_date DESC""",
            patient_id
        )

        # 3. Current medications
        medications = await conn.fetch(
            """SELECT name, dose, frequency, start_date
               FROM medications
               WHERE patient_id = $1 AND is_active = true""",
            patient_id
        )

        # 4. Recent labs (last 365 days)
        cutoff = datetime.utcnow() - timedelta(days=days_lookback)
        labs = await conn.fetch(
            """SELECT name, value, unit, result_date, in_range
               FROM lab_results
               WHERE patient_id = $1 AND result_date >= $2
               ORDER BY result_date DESC""",
            patient_id, cutoff
        )

        # 5. Vital trends
        vitals = await conn.fetch(
            """SELECT name, value, recorded_at
               FROM vitals
               WHERE patient_id = $1 AND recorded_at >= $2
               ORDER BY name, recorded_at DESC""",
            patient_id, cutoff
        )

        # 6. Care gaps
        care_gaps = await conn.fetch(
            """SELECT gap_type, last_completed, due_date, priority
               FROM care_gaps
               WHERE patient_id = $1 AND is_resolved = false""",
            patient_id
        )

        # 7. Prior deliberation knowledge (current only)
        prior_knowledge = await conn.fetch(
            """SELECT knowledge_type, entry_text, confidence,
                      valid_from, evidence_refs
               FROM patient_knowledge
               WHERE patient_id = $1
                 AND is_current = true
                 AND (valid_until IS NULL OR valid_until > NOW())
               ORDER BY created_at DESC""",
            patient_id
        )

        # 8. Upcoming appointments
        upcoming = await conn.fetch(
            """SELECT appointment_type, scheduled_at, provider_name
               FROM appointments
               WHERE patient_id = $1 AND scheduled_at > NOW()
               ORDER BY scheduled_at ASC
               LIMIT 5""",
            patient_id
        )

        # 9. Days since last encounter
        last_enc = await conn.fetchval(
            """SELECT MAX(encounter_date) FROM encounters
               WHERE patient_id = $1""",
            patient_id
        )
        days_since = (datetime.utcnow().date() - last_enc).days if last_enc else 999

    # 10. Retrieve applicable guidelines from vector store
    condition_terms = " ".join([c["display"] for c in conditions])
    med_terms = " ".join([m["name"] for m in medications])
    query = f"{condition_terms} {med_terms} management guidelines"
    applicable_guidelines = await vector_store.similarity_search(
        query=query,
        k=10,
        filter={"is_current": True}
    )

    # Group vitals by name for trend analysis
    vital_trends = {}
    for v in vitals:
        vital_trends.setdefault(v["name"], []).append(
            {"value": v["value"], "date": v["recorded_at"].isoformat()}
        )

    return PatientContextPackage(
        patient_id=patient_id,
        patient_name=f"{patient['first_name']} {patient['last_name']}",
        age=patient["age"],
        sex=patient["sex"],
        mrn=patient["mrn"],
        primary_provider=patient["primary_provider"],
        practice=patient["practice"],
        active_conditions=[dict(c) for c in conditions],
        current_medications=[dict(m) for m in medications],
        recent_labs=[dict(l) for l in labs],
        vital_trends=[{"name": k, "readings": v}
                      for k, v in vital_trends.items()],
        care_gaps=[dict(g) for g in care_gaps],
        sdoh_flags=patient.get("sdoh_flags", []),
        prior_patient_knowledge=[dict(k) for k in prior_knowledge],
        applicable_guidelines=applicable_guidelines,
        upcoming_appointments=[dict(a) for a in upcoming],
        days_since_last_encounter=days_since,
        deliberation_trigger="compiled_by_context_compiler"
    )
