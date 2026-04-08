"""
context_compiler.py — Phase 0: Compile patient context package.
Assembles EHR data, prior knowledge, and applicable guidelines
into a standardized PatientContextPackage for both analyst models.

Table mapping (actual Replit PostgreSQL schema):
  patients            — demographics (id UUID, mrn, first_name, last_name, birth_date, gender)
  patient_conditions  — conditions (patient_id UUID FK, code, display, onset_date, clinical_status)
  patient_medications — medications (patient_id UUID FK, code, display, status, authored_on)
  biometric_readings  — vitals + labs (patient_id UUID FK, metric_type, value, unit, measured_at, is_abnormal)
  clinical_events     — encounters (patient_id UUID FK, event_type, event_date, description)
  care_gaps           — gaps (patient_id UUID FK, gap_type, description, status, identified_date)
  patient_sdoh_flags  — SDoH (patient_id UUID FK, domain, flag_code, description, severity)
  patient_knowledge   — prior knowledge (patient_id TEXT=MRN, knowledge_type, entry_text, ...)

Note: patient_conditions/medications/biometrics/etc use internal UUID patient_id.
      patient_knowledge uses patient_id as TEXT (MRN).
"""
import re
import sys
from datetime import datetime, timedelta, date
from typing import Optional
from .schemas import PatientContextPackage

try:
    from ingestion.adapters.healthex.content_router import sanitize_for_context, _deep_sanitize
except ImportError:
    def sanitize_for_context(value) -> str:
        import json
        if value is None:
            return ""
        try:
            return json.loads(json.dumps(str(value)))
        except Exception:
            return str(value).encode("ascii", errors="replace").decode("ascii")

    def _deep_sanitize(obj):
        if isinstance(obj, dict):
            return {k: _deep_sanitize(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [_deep_sanitize(item) for item in obj]
        if isinstance(obj, str):
            return sanitize_for_context(obj)
        return obj

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


async def compile_patient_context(
    patient_id: str,
    db_pool,
    vector_store,
    days_lookback: int = 365
) -> PatientContextPackage:
    """
    Assemble complete patient context for deliberation.

    Args:
        patient_id: Patient MRN string (e.g. "4829341" or "MC-2025-4829")
        db_pool: asyncpg connection pool
        vector_store: pgvector client (placeholder returns empty list)
        days_lookback: how many days of history to pull

    Returns:
        PatientContextPackage validated by Pydantic
    """
    cutoff = datetime.utcnow() - timedelta(days=days_lookback)

    async with db_pool.acquire() as conn:

        # 1. Demographics — look up by MRN to get internal UUID + demographic fields
        patient = await conn.fetchrow(
            """SELECT id, mrn, first_name, last_name, birth_date, gender,
                      city, state, insurance_type
               FROM patients
               WHERE mrn = $1""",
            patient_id
        )
        if patient is None and _UUID_RE.match(patient_id):
            # Try internal UUID lookup (HealthEx patients registered via
            # register_healthex_patient return a UUID, not an MRN)
            patient = await conn.fetchrow(
                """SELECT id, mrn, first_name, last_name, birth_date, gender,
                          city, state, insurance_type
                   FROM patients
                   WHERE id = $1::uuid""",
                patient_id
            )
        if patient is None:
            # Try partial MRN match (e.g. numeric-only MRN)
            patient = await conn.fetchrow(
                """SELECT id, mrn, first_name, last_name, birth_date, gender,
                          city, state, insurance_type
                   FROM patients
                   WHERE mrn LIKE $1""",
                f"%{patient_id}%"
            )
        if patient is None:
            raise ValueError(
                f"Patient with MRN '{patient_id}' not found in patients table"
            )

        internal_id = patient["id"]          # UUID for FK lookups
        mrn_str = patient["mrn"]             # canonical MRN for patient_knowledge

        # Compute age from birth_date
        age = None
        if patient["birth_date"]:
            bd = patient["birth_date"]
            today = date.today()
            age = today.year - bd.year - (
                (today.month, today.day) < (bd.month, bd.day)
            )

        # 2. Active conditions (patient_conditions)
        conditions = await conn.fetch(
            """SELECT code, display, onset_date, clinical_status
               FROM patient_conditions
               WHERE patient_id = $1
                 AND (clinical_status IS NULL OR clinical_status != 'inactive')
               ORDER BY onset_date DESC NULLS LAST""",
            internal_id
        )

        # 3. Current medications (patient_medications)
        medications = await conn.fetch(
            """SELECT code, display, status, authored_on
               FROM patient_medications
               WHERE patient_id = $1
                 AND (status IS NULL OR status = 'active')""",
            internal_id
        )

        # 4. Recent biometric readings — vitals + labs (biometric_readings)
        biometrics = await conn.fetch(
            """SELECT metric_type, value, unit, measured_at, is_abnormal
               FROM biometric_readings
               WHERE patient_id = $1 AND measured_at >= $2
               ORDER BY metric_type, measured_at DESC""",
            internal_id, cutoff
        )

        # Split biometrics into labs vs vitals by metric_type convention
        LAB_PREFIXES = ("hba1c", "glucose", "creatinine", "cholesterol",
                        "ldl", "hdl", "triglyceride", "potassium", "sodium",
                        "egfr", "a1c", "bun")
        recent_labs = []
        vital_dict: dict = {}
        for b in biometrics:
            mtype = (b["metric_type"] or "").lower()
            if any(mtype.startswith(p) for p in LAB_PREFIXES):
                recent_labs.append({
                    "name": b["metric_type"],
                    "value": b["value"],
                    "unit": b["unit"],
                    "result_date": b["measured_at"].isoformat() if b["measured_at"] else None,
                    "in_range": not bool(b["is_abnormal"])
                })
            else:
                vital_dict.setdefault(b["metric_type"], []).append({
                    "value": b["value"],
                    "date": b["measured_at"].isoformat() if b["measured_at"] else None
                })

        vital_trends = [{"name": k, "readings": v} for k, v in vital_dict.items()]

        # 5. Care gaps (care_gaps)
        care_gaps = await conn.fetch(
            """SELECT gap_type, description, status, identified_date, resolved_date
               FROM care_gaps
               WHERE patient_id = $1 AND (status IS NULL OR status = 'open')""",
            internal_id
        )

        # 6. SDoH flags (patient_sdoh_flags)
        sdoh_rows = await conn.fetch(
            """SELECT domain, flag_code, description, severity
               FROM patient_sdoh_flags
               WHERE patient_id = $1""",
            internal_id
        )
        sdoh_flags = [dict(r) for r in sdoh_rows]

        # 7. Prior deliberation knowledge (patient_knowledge uses MRN as patient_id)
        prior_knowledge = await conn.fetch(
            """SELECT knowledge_type, entry_text, confidence,
                      valid_from, evidence_refs
               FROM patient_knowledge
               WHERE patient_id = $1
                 AND is_current = true
                 AND (valid_until IS NULL OR valid_until > NOW())
               ORDER BY created_at DESC""",
            mrn_str
        )

        # 8. Days since last clinical event
        last_event = await conn.fetchval(
            """SELECT MAX(event_date) FROM clinical_events
               WHERE patient_id = $1""",
            internal_id
        )
        if last_event:
            last_date = last_event.date() if hasattr(last_event, "date") else last_event
            days_since = (date.today() - last_date).days
        else:
            days_since = 999

        # 9. Ingestion plan summaries (from ingestion_plans if available)
        data_inventory = []
        try:
            plans = await conn.fetch(
                """SELECT resource_type, insights_summary, rows_written, status
                   FROM ingestion_plans
                   WHERE patient_id = $1 AND status = 'complete'
                   ORDER BY executed_at DESC LIMIT 20""",
                internal_id,
            )
            data_inventory = [
                {
                    "resource_type": p["resource_type"],
                    "summary": p["insights_summary"] or "",
                    "rows": p["rows_written"] or 0,
                }
                for p in plans
            ]
        except Exception:
            # Table may not exist yet — graceful fallback
            pass

        # 10. Clinical notes extracted from Binary/Observation resources
        clinical_notes_rows = []
        try:
            cn_rows = await conn.fetch(
                """SELECT note_type, note_text, note_date, author, source
                   FROM clinical_notes
                   WHERE patient_id = $1
                   ORDER BY note_date DESC NULLS LAST
                   LIMIT 20""",
                internal_id,
            )
            clinical_notes_rows = [
                {
                    "type":   sanitize_for_context(r["note_type"] or ""),
                    "text":   sanitize_for_context(r["note_text"] or ""),
                    "date":   r["note_date"].isoformat() if r["note_date"] else "",
                    "author": sanitize_for_context(r["author"] or ""),
                    "source": r["source"] or "",
                }
                for r in cn_rows
            ]
        except Exception:
            pass

        # 11. Media inventory — URL references, never raw payload
        available_media_rows = []
        try:
            mr_rows = await conn.fetch(
                """SELECT resource_type, content_type, doc_type, reference_url, doc_date
                   FROM media_references
                   WHERE patient_id = $1
                   ORDER BY doc_date DESC NULLS LAST
                   LIMIT 10""",
                internal_id,
            )
            available_media_rows = [
                {
                    "type": sanitize_for_context(r["doc_type"] or r["content_type"] or ""),
                    "url":  r["reference_url"] or "",
                    "date": r["doc_date"].isoformat() if r["doc_date"] else "",
                }
                for r in mr_rows
            ]
        except Exception:
            pass

    # 12. Applicable guidelines from vector store (placeholder returns [] gracefully)
    try:
        condition_terms = " ".join([c["display"] or "" for c in conditions if c["display"]])
        med_terms = " ".join([m["display"] or "" for m in medications if m["display"]])
        query = f"{condition_terms} {med_terms} management guidelines".strip() or "clinical guidelines"
        applicable_guidelines = await vector_store.similarity_search(
            query=query, k=10, filter={"is_current": True}
        )
    except Exception as e:
        print(f"[context_compiler] vector store skipped: {e}", file=sys.stderr)
        applicable_guidelines = []

    return PatientContextPackage(
        patient_id=patient_id,
        patient_name=f"{patient['first_name']} {patient['last_name']}",
        age=age,
        sex=patient["gender"] or "unknown",
        mrn=mrn_str,
        primary_provider="",
        practice="",
        active_conditions=[
            {
                "code": c["code"],
                "display": c["display"],
                "onset_date": c["onset_date"].isoformat() if c["onset_date"] else None,
                "clinical_status": c["clinical_status"],
            }
            for c in conditions
        ],
        current_medications=[
            {
                "code": m["code"],
                "display": m["display"],
                "status": m["status"],
                "authored_on": m["authored_on"].isoformat() if m["authored_on"] else None,
            }
            for m in medications
        ],
        recent_labs=recent_labs,
        vital_trends=vital_trends,
        care_gaps=[
            {
                "gap_type": g["gap_type"],
                "description": g["description"],
                "status": g["status"],
                "identified_date": g["identified_date"].isoformat() if g["identified_date"] else None,
            }
            for g in care_gaps
        ],
        sdoh_flags=[r.get("domain", r.get("flag_code", "")) for r in sdoh_flags],
        prior_patient_knowledge=[
            {
                "knowledge_type": k["knowledge_type"],
                "entry_text": k["entry_text"],
                "confidence": k["confidence"],
                "valid_from": k["valid_from"].isoformat() if k["valid_from"] else None,
                "evidence_refs": list(k["evidence_refs"] or []),
            }
            for k in prior_knowledge
        ],
        applicable_guidelines=applicable_guidelines,
        upcoming_appointments=[],
        days_since_last_encounter=days_since,
        deliberation_trigger="compiled_by_context_compiler",
        data_inventory=data_inventory,
        clinical_notes=clinical_notes_rows,
        available_media=available_media_rows,
    )
