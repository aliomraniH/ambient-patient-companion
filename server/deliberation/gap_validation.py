"""
gap_validation.py — Gap-aware context validation for the deliberation engine.

Pre-dispatch: detect staleness in compiled context, attempt automated refresh.
Post-deliberation: collect gap artifacts, build human-readable summary.

Integrated into engine.py between Phase 0 (context compilation) and Phase 1
(parallel analysis), and again after Phase 5 (knowledge commit).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple, Union

from .schemas import PatientContextPackage

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Freshness thresholds (duplicated from ingestion/server.py lines 68-86
# to avoid importing the FastMCP server module which has top-level side effects)
# ---------------------------------------------------------------------------

_FRESHNESS_THRESHOLDS: Dict[Tuple[str, str], Dict[str, float]] = {
    ("lab_result",      "pre_encounter"):       {"4548-4": 2160, "2160-0": 8760, "2345-7": 2160, "14959-1": 8760, "default": 4380},
    ("lab_result",      "acute_event"):         {"default": 4},
    ("lab_result",      "chronic_management"):  {"default": 4380},
    ("vital_sign",      "pre_encounter"):       {"default": 48},
    ("vital_sign",      "acute_event"):         {"default": 4},
    ("medication_list", "pre_encounter"):       {"default": 720},
    ("medication_list", "medication_change"):   {"default": 24},
    ("problem_list",    "pre_encounter"):       {"default": 8760},
    ("imaging",         "pre_encounter"):       {"default": 17520},
    ("encounter_note",  "pre_encounter"):       {"default": 2160},
}

_GUIDELINE_SOURCES: Dict[str, Tuple[str, str]] = {
    "4548-4":  ("HbA1c 90-day max", "ADA Standards of Care 2024 §6"),
    "2160-0":  ("Creatinine 365-day max", "ADA Standards of Care 2024 §10"),
    "14959-1": ("UACR 365-day max", "ADA Standards of Care 2024 §10"),
    "default": ("Standard freshness interval", "Clinical best practice"),
}

# Common lab name → LOINC code mapping for PatientContextPackage (which may
# not include LOINC codes but does include human-readable names).
_NAME_TO_LOINC: Dict[str, str] = {
    "hba1c": "4548-4",
    "hemoglobin a1c": "4548-4",
    "a1c": "4548-4",
    "creatinine": "2160-0",
    "glucose": "2345-7",
    "ldl": "2089-1",
    "hdl": "2085-9",
    "egfr": "33914-3",
    "triglycerides": "2571-8",
    "urine albumin/creatinine": "14959-1",
    "uacr": "14959-1",
}


# ---------------------------------------------------------------------------
# Trigger-to-scenario mapping
# ---------------------------------------------------------------------------

_TRIGGER_SCENARIO_MAP: Dict[str, str] = {
    "scheduled_pre_encounter": "pre_encounter",
    "pre_encounter": "pre_encounter",
    "lab_result_received": "acute_event",
    "medication_change": "medication_change",
    "missed_appointment": "chronic_management",
    "temporal_threshold": "chronic_management",
    "manual": "chronic_management",
}


def _map_trigger_to_scenario(trigger_type: str) -> str:
    """Map a deliberation trigger type to a clinical scenario for thresholds."""
    return _TRIGGER_SCENARIO_MAP.get(trigger_type, "pre_encounter")


# ---------------------------------------------------------------------------
# Context element extraction
# ---------------------------------------------------------------------------

def _extract_context_elements(
    context: Union[PatientContextPackage, dict],
) -> List[Dict[str, Any]]:
    """Extract lab/vital/medication data elements from either context type.

    Returns a list of dicts with:
      element_type, loinc_code (optional), last_updated (ISO str), source_system
    """
    elements: List[Dict[str, Any]] = []

    if isinstance(context, PatientContextPackage):
        # Full pipeline context (Pydantic model)
        for lab in context.recent_labs:
            date_str = lab.get("result_date") or lab.get("date")
            if not date_str:
                continue
            loinc = lab.get("loinc_code")
            if not loinc:
                name = (lab.get("name") or "").lower().strip()
                loinc = _NAME_TO_LOINC.get(name)
            elements.append({
                "element_type": "lab_result",
                "loinc_code": loinc,
                "last_updated": date_str,
                "source_system": "ehr",
            })

        for vital in context.vital_trends:
            readings = vital.get("readings", [])
            if readings:
                latest = max(readings, key=lambda r: r.get("date", ""))
                date_str = latest.get("date")
                if date_str:
                    elements.append({
                        "element_type": "vital_sign",
                        "loinc_code": None,
                        "last_updated": date_str,
                        "source_system": "ehr",
                    })

        if context.current_medications:
            dates = [
                m.get("authored_on") or m.get("start_date")
                for m in context.current_medications
                if m.get("authored_on") or m.get("start_date")
            ]
            if dates:
                elements.append({
                    "element_type": "medication_list",
                    "loinc_code": None,
                    "last_updated": max(dates),
                    "source_system": "ehr",
                })

    elif isinstance(context, dict):
        # Progressive mode context (plain dict from TieredContextLoader)
        for lab in context.get("recent_labs", []):
            date_str = lab.get("date")
            if not date_str:
                continue
            name = (lab.get("test") or lab.get("name") or "").lower().strip()
            loinc = _NAME_TO_LOINC.get(name)
            elements.append({
                "element_type": "lab_result",
                "loinc_code": loinc,
                "last_updated": date_str,
                "source_system": "ehr",
            })

        for vital in context.get("vital_trends", []):
            readings = vital.get("readings", [])
            if readings:
                latest = max(readings, key=lambda r: r.get("date", ""))
                date_str = latest.get("date")
                if date_str:
                    elements.append({
                        "element_type": "vital_sign",
                        "loinc_code": None,
                        "last_updated": date_str,
                        "source_system": "ehr",
                    })

    return elements


# ---------------------------------------------------------------------------
# Internal staleness detection (no MCP/HTTP needed)
# ---------------------------------------------------------------------------

def detect_staleness_internal(
    context_elements: List[Dict[str, Any]],
    clinical_scenario: str,
) -> Dict[str, Any]:
    """Detect stale context elements using clinically-defined thresholds.

    Pure-Python version of ingestion/server.py:detect_context_staleness.
    """
    stale: List[Dict[str, Any]] = []
    now = datetime.now(timezone.utc)

    for el in context_elements:
        el_type = el.get("element_type", "lab_result")
        loinc = el.get("loinc_code")
        updated_str = el.get("last_updated")
        if not updated_str:
            continue

        try:
            if "T" in updated_str or "+" in updated_str:
                updated = datetime.fromisoformat(
                    updated_str.replace("Z", "+00:00")
                )
            else:
                updated = datetime.fromisoformat(updated_str + "T00:00:00+00:00")
        except (ValueError, TypeError):
            continue

        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=timezone.utc)

        age_hours = (now - updated).total_seconds() / 3600

        scenario_thresholds = _FRESHNESS_THRESHOLDS.get(
            (el_type, clinical_scenario),
            _FRESHNESS_THRESHOLDS.get((el_type, "pre_encounter"), {"default": 4380}),
        )
        max_hours = scenario_thresholds.get(
            loinc or "default", scenario_thresholds["default"]
        )

        if age_hours > max_hours:
            rationale, source = _GUIDELINE_SOURCES.get(
                loinc or "default", _GUIDELINE_SOURCES["default"]
            )
            stale.append({
                "element_type": el_type,
                "loinc_code": loinc,
                "age_hours": round(age_hours, 1),
                "max_acceptable_age_hours": max_hours,
                "clinical_rationale": rationale,
                "guideline_source": source,
            })

    total = len(context_elements)
    stale_count = len(stale)
    freshness_score = round(1.0 - (stale_count / max(total, 1)), 2)

    return {
        "stale_elements": stale,
        "freshness_score": freshness_score,
        "recommended_refreshes": [
            f"Refresh {s['element_type']}"
            + (f" (LOINC {s['loinc_code']})" if s["loinc_code"] else "")
            for s in stale
            if s["age_hours"] > s["max_acceptable_age_hours"] * 1.5
        ],
    }


# ---------------------------------------------------------------------------
# Refresh stale data using the engine's existing db_pool
# ---------------------------------------------------------------------------

async def refresh_stale_data(
    db_pool,
    patient_id: str,
    stale_elements: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Search warehouse for fresher versions of stale data elements.

    Uses the engine's existing db_pool (not gap_aware.db's singleton).
    """
    found: List[Dict[str, Any]] = []

    # Resolve MRN → UUID (same 3-step logic as context_compiler.py:75-104)
    import re
    uuid_re = re.compile(
        r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
        re.IGNORECASE,
    )

    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id FROM patients WHERE mrn = $1", patient_id
        )
        if not row and uuid_re.match(patient_id):
            row = await conn.fetchrow(
                "SELECT id FROM patients WHERE id = $1::uuid", patient_id
            )
        if not row:
            row = await conn.fetchrow(
                "SELECT id FROM patients WHERE mrn LIKE $1",
                f"%{patient_id}%",
            )
        if not row:
            return found

        patient_uuid = str(row["id"])

        for el in stale_elements:
            loinc = el.get("loinc_code")
            if not loinc:
                continue

            cutoff = datetime.now(timezone.utc) - timedelta(days=180)
            cache_row = await conn.fetchrow(
                """
                SELECT raw_json, retrieved_at, source_name
                FROM raw_fhir_cache
                WHERE patient_id = $1::uuid
                  AND raw_json::text LIKE $2
                  AND retrieved_at > $3
                ORDER BY retrieved_at DESC
                LIMIT 1
                """,
                patient_uuid,
                f"%{loinc}%",
                cutoff,
            )

            if cache_row:
                found.append({
                    "element_type": el.get("element_type", "lab_result"),
                    "value": "found_in_cache",
                    "unit": None,
                    "effective_date": cache_row["retrieved_at"].isoformat(),
                    "source_system": cache_row["source_name"] or "warehouse",
                    "provenance": "raw_fhir_cache",
                    "normalized": False,
                })

    return found


# ---------------------------------------------------------------------------
# Inject fresh data back into context
# ---------------------------------------------------------------------------

def _inject_fresh_data(
    context: Union[PatientContextPackage, dict],
    fresh_elements: List[Dict[str, Any]],
) -> Union[PatientContextPackage, dict]:
    """Merge refreshed data back into the context."""
    if not fresh_elements:
        return context

    if isinstance(context, PatientContextPackage):
        for el in fresh_elements:
            context.recent_labs.append({
                "name": el.get("element_type", "refreshed_lab"),
                "value": el.get("value", ""),
                "unit": el.get("unit", ""),
                "result_date": el.get("effective_date"),
                "in_range": None,
                "loinc_code": None,
                "source": "gap_validation_refresh",
            })
    elif isinstance(context, dict):
        context["_refreshed_data"] = fresh_elements

    return context


# ---------------------------------------------------------------------------
# Gap summary builder
# ---------------------------------------------------------------------------

def build_gap_summary(gaps: List[Dict[str, Any]]) -> str:
    """Build a human-readable summary of critical/high gaps."""
    critical = [g for g in gaps if g.get("severity") == "critical"]
    high = [g for g in gaps if g.get("severity") == "high"]

    parts: List[str] = []
    if critical:
        descs = "; ".join(
            (g.get("description") or "")[:80] for g in critical
        )
        parts.append(f"{len(critical)} critical gap(s): {descs}")
    if high:
        descs = "; ".join(
            (g.get("description") or "")[:80] for g in high
        )
        parts.append(f"{len(high)} high gap(s): {descs}")

    return " | ".join(parts) if parts else ""


# ---------------------------------------------------------------------------
# Main entry points
# ---------------------------------------------------------------------------

async def validate_and_enrich_context(
    context: Union[PatientContextPackage, dict],
    db_pool,
    patient_id: str,
    trigger_type: str,
) -> Tuple[Union[PatientContextPackage, dict], Dict[str, Any]]:
    """Pre-dispatch context validation: detect staleness, attempt refresh.

    Returns (enriched_context, validation_metadata).
    Non-fatal: on any error, returns original context with empty metadata.
    """
    meta: Dict[str, Any] = {
        "freshness_score": 1.0,
        "stale_elements_detected": 0,
        "elements_refreshed": 0,
        "refresh_attempted": False,
        "context_validated_at": datetime.now(timezone.utc).isoformat(),
    }

    try:
        elements = _extract_context_elements(context)
        if not elements:
            return context, meta

        scenario = _map_trigger_to_scenario(trigger_type)
        staleness = detect_staleness_internal(elements, scenario)
        meta["freshness_score"] = staleness["freshness_score"]
        meta["stale_elements_detected"] = len(staleness["stale_elements"])

        if staleness["stale_elements"] and staleness["freshness_score"] < 0.6:
            meta["refresh_attempted"] = True
            fresh = await refresh_stale_data(
                db_pool, patient_id, staleness["stale_elements"]
            )
            meta["elements_refreshed"] = len(fresh)
            if fresh:
                context = _inject_fresh_data(context, fresh)

    except Exception as exc:
        log.warning("validate_and_enrich_context failed (non-fatal): %s", exc)

    return context, meta


async def collect_gap_artifacts(
    db_pool,
    deliberation_id: str,
) -> Tuple[List[Dict[str, Any]], str]:
    """Post-deliberation: collect gap artifacts and build summary.

    Queries reasoning_gaps directly using the engine's db_pool.
    Returns (gap_list, gap_summary_text).
    """
    gaps: List[Dict[str, Any]] = []
    try:
        severity_order = (
            "CASE severity "
            "WHEN 'critical' THEN 1 WHEN 'high' THEN 2 "
            "WHEN 'medium' THEN 3 ELSE 4 END"
        )
        async with db_pool.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT * FROM reasoning_gaps WHERE deliberation_id = $1 "
                f"ORDER BY {severity_order}",
                deliberation_id,
            )
        gaps = [dict(r) for r in rows]
        # Convert non-serializable types for JSON safety
        for g in gaps:
            for k, v in list(g.items()):
                if isinstance(v, datetime):
                    g[k] = v.isoformat()
                elif hasattr(v, "__str__") and not isinstance(v, (str, int, float, bool, type(None), list, dict)):
                    g[k] = str(v)
    except Exception as exc:
        log.warning("collect_gap_artifacts query failed (non-fatal): %s", exc)

    summary = build_gap_summary(gaps)
    return gaps, summary
