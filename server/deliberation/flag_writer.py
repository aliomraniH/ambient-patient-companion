"""
flag_writer.py — writes deliberation flags to the flag registry with
full data provenance and quality scoring.

Called at the end of each deliberation round to write flags to
deliberation_flags instead of (or in addition to) deliberation_outputs.
"""

import hashlib
import json
import logging
from typing import Optional

log = logging.getLogger(__name__)

# Flag basis inference rules
FLAG_BASIS_PATTERNS = {
    # Data corruption indicators
    "0.0": "data_corrupt",
    "placeholder": "data_corrupt",
    "transmission error": "data_corrupt",
    "all zero": "data_corrupt",
    "data integrity": "data_corrupt",
    # Missing data indicators
    "not documented": "data_missing",
    "absent": "data_missing",
    "no recent": "data_missing",
    "missing": "data_missing",
    "undocumented": "data_missing",
    "unavailable": "data_missing",
    # Stale data indicators
    "months ago": "data_stale",
    "years ago": "data_stale",
    "outdated": "data_stale",
    "2.5-year": "data_stale",
    "16-month": "data_stale",
    "286 days": "data_stale",
    # Conflict indicators
    "contradicts": "data_conflict",
    "inconsistent": "data_conflict",
    "duplicate": "data_conflict",
    "mismatch": "data_conflict",
}


def infer_flag_basis(flag_text: str) -> str:
    """Infer the flag_basis from the flag description text."""
    text_lower = flag_text.lower()
    for pattern, basis in FLAG_BASIS_PATTERNS.items():
        if pattern in text_lower:
            return basis
    return "clinical_finding"


def compute_flag_fingerprint(patient_id: str, title: str, basis: str) -> str:
    """SHA-256 fingerprint for deduplication across deliberation runs."""
    raw = f"{patient_id}::{title.strip().lower()}::{basis}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def score_data_quality(provenance: list[dict]) -> float:
    """
    0.0 = all records are suspect (0.0 values, missing)
    1.0 = all records are valid
    """
    if not provenance:
        return 1.0
    suspect = sum(1 for p in provenance if p.get("is_suspect", False))
    return round(1.0 - (suspect / len(provenance)), 2)


async def collect_data_provenance(
    conn, patient_id: str, flag_text: str
) -> list[dict]:
    """
    Identify which specific DB records likely caused this flag.
    Returns provenance records for the data_provenance JSONB column.
    """
    provenance = []
    text_lower = flag_text.lower()

    # Check if flag references lab values
    if any(t in text_lower for t in ["a1c", "glucose", "lab", "hba1c", "value", "ldl"]):
        recent_labs = await conn.fetch(
            """SELECT id, metric_type, value, unit, measured_at
               FROM biometric_readings
               WHERE patient_id = $1::uuid
               ORDER BY measured_at DESC LIMIT 5""",
            patient_id,
        )
        for lab in recent_labs:
            provenance.append({
                "table": "biometric_readings",
                "record_id": str(lab["id"]),
                "field": "value",
                "value_at_flag_time": str(lab.get("value", "")),
                "metric_type": lab.get("metric_type", ""),
                "is_suspect": lab.get("value") in (None, 0.0, 0),
            })

    # Check if flag references conditions
    if any(t in text_lower for t in ["prediabetes", "diagnosis", "condition", "active"]):
        conditions = await conn.fetch(
            """SELECT id, display, clinical_status, onset_date
               FROM patient_conditions
               WHERE patient_id = $1::uuid
               ORDER BY onset_date DESC NULLS LAST LIMIT 5""",
            patient_id,
        )
        for cond in conditions:
            provenance.append({
                "table": "patient_conditions",
                "record_id": str(cond["id"]),
                "field": "clinical_status",
                "value_at_flag_time": cond.get("clinical_status", ""),
                "condition_name": cond.get("display", ""),
                "is_suspect": False,
            })

    return provenance


async def write_flag(
    conn,
    patient_id: str,
    deliberation_id: str,
    flag_data: dict,
) -> dict:
    """
    Write a single deliberation flag to the registry.
    Deduplicates by fingerprint — if an identical open flag already exists,
    returns the existing flag_id rather than creating a duplicate.
    """
    title = flag_data.get("flag") or flag_data.get("title") or "Unnamed flag"
    description = (
        flag_data.get("description")
        or flag_data.get("detail")
        or title
    )
    priority_raw = (flag_data.get("priority") or "medium").lower().replace(" ", "-")
    valid_priorities = {"low", "medium", "medium-high", "high", "critical"}
    priority = priority_raw if priority_raw in valid_priorities else "medium"

    basis = infer_flag_basis(f"{title} {description}")
    fingerprint = compute_flag_fingerprint(patient_id, title, basis)
    provenance = await collect_data_provenance(conn, patient_id, f"{title} {description}")
    quality = score_data_quality(provenance)

    # Check for existing open flag with same fingerprint
    existing = await conn.fetchrow(
        """SELECT id FROM deliberation_flags
           WHERE patient_id = $1::uuid
             AND flag_fingerprint = $2
             AND lifecycle_state = 'open'""",
        patient_id, fingerprint,
    )

    if existing:
        await conn.execute(
            """UPDATE deliberation_flags
               SET deliberation_id = $1::uuid,
                   data_provenance = $2::jsonb,
                   data_quality_score = $3,
                   had_zero_values = $4,
                   reviewed_at = NOW()
               WHERE id = $5""",
            deliberation_id,
            json.dumps(provenance),
            quality,
            quality < 0.5,
            existing["id"],
        )
        return {"flag_id": str(existing["id"]), "action": "updated_existing"}

    flag_id = await conn.fetchval(
        """INSERT INTO deliberation_flags (
               patient_id, deliberation_id, flag_type,
               title, description, priority, flag_basis,
               data_provenance, data_quality_score,
               had_zero_values, had_missing_fields,
               flag_fingerprint
           ) VALUES ($1::uuid, $2::uuid, $3, $4, $5,
                     $6::flag_priority, $7::flag_basis,
                     $8::jsonb, $9, $10, $11, $12)
           RETURNING id""",
        patient_id,
        deliberation_id,
        flag_data.get("flag_type", "missing_data_flag"),
        title,
        description,
        priority,
        basis,
        json.dumps(provenance),
        quality,
        quality < 0.5,
        "missing" in description.lower(),
        fingerprint,
    )

    return {"flag_id": str(flag_id), "action": "created"}
