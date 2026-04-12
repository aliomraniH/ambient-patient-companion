"""
CRITICAL VALUE INJECTOR — Gold Context Compilation
===================================================
Guarantees that specific high-safety lab values appear in every Gold context
regardless of what the summarizer chose to include. Operates as a post-process
on the compiled context, injecting directly from biometric_readings.

This fixes the known bug where compile_patient_context omits creatinine, blood
pressure, and other safety-critical values despite them existing in the DB.

LOINC codes targeted: those identified as required for the agent council's
deliberation — medications safety, risk stratification, care gap detection.
"""

import json
import logging
import re
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger(__name__)

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)

# LOINC codes whose values MUST appear in Gold context
CRITICAL_LOINC_CODES: dict[str, str] = {
    "4548-4":  "hba1c_percent",           # ADA-required for T2DM management
    "2160-0":  "creatinine_mgdl",          # Required for metformin, RAAS safety
    "33914-3": "egfr",                     # Required for CKD staging, drug dosing
    "14959-1": "uacr_mgg",                 # ADA annual diabetes screening
    "55284-4": "systolic_bp_mmhg",         # HTN management
    "8462-4":  "diastolic_bp_mmhg",        # HTN management
    "2345-7":  "fasting_glucose_mgdl",     # Diabetes monitoring
    "39156-5": "bmi",                      # Metabolic risk
    "2823-3":  "potassium_meql",           # RAAS / diuretic safety
    "2085-9":  "hdl_mgdl",                 # Cardiovascular risk
    "13457-7": "ldl_mgdl",                 # Statin therapy guidance
}

# Conditions that require specific LOINC values to be present
CONDITION_REQUIRED_VALUES: dict[str, list[str]] = {
    "E11":  ["4548-4", "2160-0", "33914-3", "14959-1"],  # T2DM requires these 4
    "I10":  ["55284-4", "8462-4", "2823-3"],              # HTN requires BP + K+
    "F41":  [],                                            # GAD — no specific labs
    "N18":  ["2160-0", "33914-3", "2823-3"],              # CKD
    "R73":  ["4548-4", "2345-7"],                         # Prediabetes
    "E78":  ["2085-9", "13457-7"],                        # Hyperlipidemia
}


async def inject_critical_values(
    context,  # PatientContextPackage
    db_pool,
    patient_id: str,
) -> object:
    """
    After context compilation, inject critical lab values directly from DB.
    Results are placed into context.applicable_guidelines as a synthetic entry
    with source "__critical_values__" where they are guaranteed visible.

    Also populates "__missing_critical__" for gap detection to act on.

    Args:
        context: PatientContextPackage instance
        db_pool: asyncpg connection pool
        patient_id: Patient MRN or UUID string

    Returns:
        Modified context with critical values injected
    """
    try:
        async with db_pool.acquire() as conn:
            # Resolve patient UUID
            patient_uuid = await _resolve_patient_uuid(conn, patient_id)
            if patient_uuid is None:
                log.warning("[CRITICAL_VALUES] Patient %s not found", patient_id)
                return context

            # Fetch all critical labs in one efficient query
            loinc_codes = list(CRITICAL_LOINC_CODES.keys())
            critical_labs = await conn.fetch(
                """SELECT DISTINCT ON (loinc_code)
                          metric_type, value, unit, measured_at, is_abnormal,
                          loinc_code
                   FROM biometric_readings
                   WHERE patient_id = $1
                     AND loinc_code = ANY($2)
                   ORDER BY loinc_code, measured_at DESC""",
                patient_uuid, loinc_codes,
            )

            # Build critical values dict
            critical_values: dict[str, dict] = {}
            found_loincs: set[str] = set()

            for row in critical_labs:
                loinc = row["loinc_code"]
                field_name = CRITICAL_LOINC_CODES[loinc]
                found_loincs.add(loinc)
                critical_values[field_name] = {
                    "value": row["value"],
                    "unit": row["unit"] or "",
                    "date": row["measured_at"].isoformat() if row["measured_at"] else None,
                    "age_days": _age_in_days(row["measured_at"]),
                    "loinc": loinc,
                    "is_abnormal": bool(row["is_abnormal"]),
                }

            # Identify missing critical values
            missing_critical = [
                CRITICAL_LOINC_CODES[lc]
                for lc in loinc_codes
                if lc not in found_loincs
            ]

            # Check condition-specific gaps
            condition_gaps = _compute_condition_gaps(context, found_loincs)

            # Inject into context as synthetic guideline entries
            injection = {
                "critical_values": critical_values,
                "missing_critical": missing_critical,
                "condition_gaps": condition_gaps,
                "values_guaranteed_present": list(critical_values.keys()),
            }

            context.applicable_guidelines.append({
                "source": "__critical_values__",
                "content": json.dumps(injection, default=str),
            })

            if missing_critical:
                context.applicable_guidelines.append({
                    "source": "__missing_critical__",
                    "content": json.dumps({
                        "missing_values": missing_critical,
                        "condition_gaps": condition_gaps,
                        "action": "Flag for care gap detection",
                    }),
                })

            log.info(
                "[CRITICAL_VALUES] Injected %d values, %d missing, %d condition gaps for %s",
                len(critical_values), len(missing_critical), len(condition_gaps), patient_id,
            )

    except Exception as e:
        log.warning("[CRITICAL_VALUES] Injection failed (non-fatal): %s", e)

    return context


def _compute_condition_gaps(context, found_loincs: set[str]) -> list[dict]:
    """Identify condition-required values that are missing."""
    condition_gaps = []

    # Get active ICD-10 codes from context
    active_conditions = getattr(context, "active_conditions", [])
    icd10_codes = []
    for cond in active_conditions:
        code = cond.get("code", "") if isinstance(cond, dict) else ""
        if code:
            icd10_codes.append(code)

    for icd10 in icd10_codes:
        prefix = icd10[:3]  # E11, I10, etc.
        required = CONDITION_REQUIRED_VALUES.get(prefix, [])
        for loinc in required:
            if loinc not in found_loincs:
                field = CRITICAL_LOINC_CODES.get(loinc, loinc)
                condition_gaps.append({
                    "condition": icd10,
                    "missing_loinc": loinc,
                    "missing_field": field,
                    "clinical_rationale": f"Required for {prefix} management per guidelines",
                })

    return condition_gaps


async def _resolve_patient_uuid(conn, patient_id: str):
    """Resolve patient MRN or UUID to internal UUID."""
    # Try MRN lookup first
    result = await conn.fetchval(
        "SELECT id FROM patients WHERE mrn = $1", patient_id
    )
    if result:
        return result

    # Try UUID direct lookup
    if _UUID_RE.match(patient_id):
        result = await conn.fetchval(
            "SELECT id FROM patients WHERE id = $1::uuid", patient_id
        )
        if result:
            return result

    # Try partial MRN match
    result = await conn.fetchval(
        "SELECT id FROM patients WHERE mrn LIKE $1", f"%{patient_id}%"
    )
    return result


def _age_in_days(measured_at) -> Optional[int]:
    """Compute age in days from a timestamp."""
    if measured_at is None:
        return None
    if isinstance(measured_at, str):
        measured_at = datetime.fromisoformat(measured_at.replace("Z", "+00:00"))
    if hasattr(measured_at, "tzinfo") and measured_at.tzinfo is None:
        measured_at = measured_at.replace(tzinfo=timezone.utc)
    try:
        return (datetime.now(timezone.utc) - measured_at).days
    except TypeError:
        return None
