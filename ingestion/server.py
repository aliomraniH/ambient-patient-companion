"""FastMCP server entry point for the Data Ingestion Service."""

import logging
import os
import sys

logging.basicConfig(level=logging.INFO, stream=sys.stderr)
logger = logging.getLogger(__name__)

from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

mcp = FastMCP("ambient-ingestion")


@mcp.custom_route("/health", methods=["GET"])
async def rest_health(request: Request) -> JSONResponse:
    return JSONResponse({"ok": True, "server": "ambient-ingestion", "version": "1.0.0"})


@mcp.tool
async def trigger_ingestion(
    patient_id: str,
    source: str = "synthea",
    force_refresh: bool = False,
) -> str:
    """Trigger the ingestion pipeline for a patient.

    Args:
        patient_id: UUID of the patient
        source: Data source adapter name (synthea | healthex)
        force_refresh: Skip freshness check and always re-ingest
    """
    try:
        import asyncpg
        from ingestion.pipeline import IngestionPipeline

        database_url = os.environ.get("DATABASE_URL", "")
        pool = await asyncpg.create_pool(database_url, min_size=1, max_size=5)

        try:
            pipeline = IngestionPipeline(adapter_name=source, pool=pool)
            result = await pipeline.run(
                patient_id=patient_id,
                force_refresh=force_refresh,
                triggered_by="force_refresh" if force_refresh else "schedule",
            )
            return (
                f"OK Ingestion {result.status} | "
                f"{result.records_upserted} records | "
                f"{result.conflicts_detected} conflicts | "
                f"{result.duration_ms}ms"
            )
        finally:
            await pool.close()

    except Exception as e:
        logger.error("trigger_ingestion failed: %s", e)
        return f"Error: {e}"


# ---------------------------------------------------------------------------
# Gap-aware tools
# ---------------------------------------------------------------------------

# Freshness thresholds in hours keyed by (element_type, clinical_scenario)
_FRESHNESS_THRESHOLDS = {
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

_GUIDELINE_SOURCES = {
    "4548-4":  ("HbA1c 90-day max", "ADA Standards of Care 2024 §6"),
    "2160-0":  ("Creatinine 365-day max", "ADA Standards of Care 2024 §10"),
    "14959-1": ("UACR 365-day max", "ADA Standards of Care 2024 §10"),
    "default": ("Standard freshness interval", "Clinical best practice"),
}


@mcp.tool
async def detect_context_staleness(
    patient_mrn: str,
    context_elements: list,
    clinical_scenario: str,
) -> str:
    """Scan compiled context for data elements whose age exceeds clinically-defined
    freshness thresholds. Call before dispatching context to any agent.

    Args:
        patient_mrn: Patient MRN
        context_elements: List of dicts with element_type, loinc_code, last_updated (ISO), source_system
        clinical_scenario: pre_encounter | acute_event | chronic_management | medication_change | discharge_planning
    """
    import json
    from datetime import datetime, timezone

    stale = []
    now = datetime.now(timezone.utc)

    for el in context_elements:
        el_type = el.get("element_type", "lab_result")
        loinc = el.get("loinc_code")
        updated_str = el.get("last_updated")
        if not updated_str:
            continue

        updated = datetime.fromisoformat(updated_str.replace("Z", "+00:00"))
        age_hours = (now - updated).total_seconds() / 3600

        scenario_thresholds = _FRESHNESS_THRESHOLDS.get(
            (el_type, clinical_scenario),
            _FRESHNESS_THRESHOLDS.get((el_type, "pre_encounter"), {"default": 4380}),
        )
        max_hours = scenario_thresholds.get(loinc or "default", scenario_thresholds["default"])

        if age_hours > max_hours:
            rationale, source = _GUIDELINE_SOURCES.get(loinc or "default", _GUIDELINE_SOURCES["default"])
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

    return json.dumps({
        "stale_elements": stale,
        "freshness_score": freshness_score,
        "recommended_refreshes": [
            f"Refresh {s['element_type']}" + (f" (LOINC {s['loinc_code']})" if s["loinc_code"] else "")
            for s in stale
            if s["age_hours"] > s["max_acceptable_age_hours"] * 1.5
        ],
    })


@mcp.tool
async def search_patient_data_extended(
    patient_mrn: str,
    search_scope: list,
    data_elements: list,
    gap_id: str = "",
    fhir_query_override: str = "",
) -> str:
    """Search beyond the pre-compiled context window for patient data in the full
    warehouse history, pharmacy claims, or HIE-connected external sources.

    Args:
        patient_mrn: Patient MRN
        search_scope: List of scopes: warehouse_full_history | pharmacy_claims |
                      hie_network | external_labs | patient_reported | wearable_telemetry
        data_elements: List of dicts with element_type, loinc_code, rxnorm_code, lookback_days
        gap_id: Optional gap_id to associate results with
        fhir_query_override: Optional raw FHIR query string
    """
    import json
    import asyncpg
    from gap_aware.db import resolve_mrn_to_uuid

    database_url = os.environ.get("DATABASE_URL", "")
    patient_uuid = await resolve_mrn_to_uuid(patient_mrn)
    found = []
    not_found = []

    if not patient_uuid:
        return json.dumps({
            "found_elements": [],
            "not_found": [f"Patient MRN {patient_mrn} not found"],
            "gap_resolved": False,
        })

    pool = await asyncpg.create_pool(database_url, min_size=1, max_size=3)
    try:
        async with pool.acquire() as conn:
            for el in data_elements:
                loinc = el.get("loinc_code")
                lookback = el.get("lookback_days", 365)
                el_type = el.get("element_type", "lab_result")
                found_this = False

                if "warehouse_full_history" in search_scope and loinc:
                    from datetime import datetime, timedelta, timezone
                    cutoff = datetime.now(timezone.utc) - timedelta(days=lookback)
                    row = await conn.fetchrow(
                        """
                        SELECT raw_json, retrieved_at, source_name
                        FROM raw_fhir_cache
                        WHERE patient_id = $1::uuid
                          AND raw_json::text LIKE $2
                          AND retrieved_at > $3
                        ORDER BY retrieved_at DESC
                        LIMIT 1
                        """,
                        patient_uuid, f"%{loinc}%", cutoff,
                    )

                    if row:
                        found.append({
                            "element_type": el_type,
                            "value": "found_in_cache",
                            "unit": None,
                            "effective_date": row["retrieved_at"].isoformat(),
                            "source_system": row["source_name"] or "warehouse",
                            "provenance": "raw_fhir_cache",
                            "normalized": False,
                        })
                        found_this = True

                if not found_this:
                    not_found.append(
                        f"{el_type}" + (f" (LOINC {loinc})" if loinc else "")
                    )
    finally:
        await pool.close()

    gap_resolved = len(found) > 0 and len(not_found) == 0

    return json.dumps({
        "found_elements": found,
        "not_found": not_found,
        "gap_resolved": gap_resolved,
    }, default=str)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    transport = os.environ.get("MCP_TRANSPORT", "stdio")
    host = os.environ.get("MCP_HOST", "0.0.0.0")
    port = int(os.environ.get("MCP_PORT", "8003"))
    if transport == "streamable-http":
        mcp.run(transport="streamable-http", host=host, port=port)
    else:
        mcp.run(transport="stdio")
