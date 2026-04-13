"""Skill: ingestion tools — freshness checks, ingestion triggers, conflict queries,
and freshness-gated orchestration pipeline.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import uuid
from datetime import datetime

from fastmcp import FastMCP

from db.connection import get_pool
from skills.base import get_data_track, log_skill_execution
from transforms.fhir_to_schema import _parse_date

logging.basicConfig(level=logging.INFO, stream=sys.stderr)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Freshness TTL defaults (hours) for each orchestration phase
# ---------------------------------------------------------------------------

FRESHNESS_TTL: dict[str, int] = {
    "compute_obt_score": 24,
    "compute_provider_risk": 24,
    "deliberation": 12,
    "generate_previsit_brief": 24,
}


# ---------------------------------------------------------------------------
# Freshness query helpers
# ---------------------------------------------------------------------------


async def _get_skill_freshness(
    conn, patient_id: str, skill_name: str,
) -> datetime | None:
    """Return the latest successful execution timestamp for a skill."""
    row = await conn.fetchrow(
        """
        SELECT execution_date FROM skill_executions
        WHERE patient_id = $1 AND skill_name = $2 AND status = 'completed'
        ORDER BY execution_date DESC LIMIT 1
        """,
        patient_id, skill_name,
    )
    return row["execution_date"] if row else None


async def _get_deliberation_freshness(
    conn, patient_id: str,
) -> datetime | None:
    """Return the latest completed deliberation timestamp for a patient."""
    row = await conn.fetchrow(
        """
        SELECT triggered_at FROM deliberations
        WHERE patient_id = $1 AND status = 'complete'
        ORDER BY triggered_at DESC LIMIT 1
        """,
        patient_id,
    )
    return row["triggered_at"] if row else None


def _is_stale(last_run: datetime | None, ttl_hours: int) -> bool:
    """Return True if *last_run* is None or older than *ttl_hours*."""
    if last_run is None:
        return True
    naive = last_run.replace(tzinfo=None) if last_run.tzinfo else last_run
    elapsed = (datetime.utcnow() - naive).total_seconds() / 3600
    return elapsed >= ttl_hours


# ---------------------------------------------------------------------------
# HealthEx payload explosion helpers
# ---------------------------------------------------------------------------

_HX_CONTAINER_KEYS: dict[str, list[str]] = {
    "conditions":  ["conditions", "Conditions", "problems", "diagnoses"],
    "medications": ["medications", "Medications", "drugs", "prescriptions"],
    "labs":        ["labs", "labResults", "lab_results", "observations",
                    "Labs", "results"],
    "encounters":  ["encounters", "visits", "Encounters", "Visits",
                    "appointments"],
}


def _explode_fhir_bundle(data: object, resource_type: str = "") -> list[dict]:
    """Flatten any HealthEx payload into a list of individual resource dicts.

    Resolution order:
      1. FHIR Bundle  → extract entry[*].resource
      2. HealthEx container dict (e.g. {"labs": [...]})  → inner list
      3. Generic container (any value that is a list of dicts)
      4. Plain list   → use as-is
      5. Single dict  → wrap in [data]
    """
    if isinstance(data, dict):
        if data.get("resourceType") == "Bundle":
            return [
                e["resource"]
                for e in data.get("entry", [])
                if isinstance(e.get("resource"), dict)
            ]
        for key in _HX_CONTAINER_KEYS.get(resource_type, []):
            if key in data and isinstance(data[key], list):
                return data[key]
        for val in data.values():
            if isinstance(val, list) and val and isinstance(val[0], dict):
                return val
        return [data]
    if isinstance(data, list):
        return data
    return []


def _parse_lab_value(raw_value: str) -> float:
    """Extract a numeric value from a lab result string.

    Handles plain numbers, ranges like "34.0-34.9", and prefixed
    values like ">60" or "<5".  Returns 0.0 when no number is found.
    """
    if not raw_value:
        return 0.0
    m = re.search(r"[\d.]+", str(raw_value))
    return float(m.group()) if m else 0.0


def _native_to_warehouse_rows(
    rows: list[dict],
    resource_type: str,
    patient_id: str,
) -> list[dict]:
    """Map adaptive_parse native dicts to warehouse-schema rows.

    The returned dicts have exactly the keys the Stage 7 per-table
    INSERT statements expect, so they can be fed directly into the
    existing write loop.
    """
    mapped: list[dict] = []
    for row in rows:
        rec: dict = {
            "id": str(uuid.uuid4()),
            "patient_id": patient_id,
            "data_source": "healthex",
        }
        if resource_type == "labs":
            rec["metric_type"] = row.get("test_name") or row.get("name", "")
            rec["value"] = _parse_lab_value(row.get("value", ""))
            rec["unit"] = row.get("unit", "")
            rec["measured_at"] = _parse_date(
                row.get("date") or row.get("effective_date", "")
            )
        elif resource_type == "conditions":
            rec["code"] = row.get("code") or row.get("icd10", "")
            rec["display"] = row.get("name", "")
            rec["system"] = ""
            rec["onset_date"] = _parse_date(row.get("onset_date", ""))
            rec["clinical_status"] = row.get("status", "active")
        elif resource_type == "medications":
            rec["code"] = row.get("code", "")
            rec["display"] = row.get("display") or row.get("name", "")
            rec["system"] = ""
            rec["status"] = row.get("status", "active")
            rec["authored_on"] = _parse_date(row.get("start_date", ""))
        elif resource_type == "encounters":
            rec["event_type"] = row.get("type", "")
            rec["event_date"] = _parse_date(row.get("date", ""))
            rec["description"] = row.get("description", "")
            rec["source_system"] = row.get("provider", "")
        else:
            continue

        # Skip rows where all resource-specific fields are empty
        skip_keys = {"id", "patient_id", "data_source"}
        if not any(v for k, v in rec.items() if k not in skip_keys):
            continue

        mapped.append(rec)
    return mapped


async def _set_data_track(track: str, tool_name: str) -> str:
    """Persist the active data track to system_config.

    Args:
        track: "synthea" or "healthex"
        tool_name: name of the calling tool (for audit log)

    Returns:
        The track value that was set.

    Raises:
        Exception: on database errors.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO system_config (key, value, updated_at)
            VALUES ($1, $2, NOW())
            ON CONFLICT (key)
            DO UPDATE SET value = $2, updated_at = NOW()
            """,
            "DATA_TRACK", track,
        )
        await log_skill_execution(
            conn, tool_name, None, "completed",
            output_data={"track": track},
        )
    return track


async def register_healthex_patient(
    health_summary_json: str,
    mrn_override: str = "",
) -> str:
    """Register a real HealthEx patient in the warehouse.

    MUST be called before any ingest_from_healthex calls in a HealthEx
    session. Takes the raw JSON from HealthEx get_health_summary, creates
    or finds the patient row with is_synthetic=False, initialises
    data_sources and source_freshness rows, and returns the canonical
    patient_id UUID and MRN for all subsequent calls.

    Also sets DATA_TRACK = "healthex" in system_config so all future
    pipeline runs use the HealthEx adapter.

    Args:
        health_summary_json: Raw JSON string from HealthEx get_health_summary.
                             May be a FHIR Patient resource, a FHIR Bundle,
                             or a HealthEx summary dict — all are handled.
        mrn_override:        If provided, use this MRN instead of extracting
                             from the summary.

    Returns:
        JSON string with patient_id, mrn, and status.
    """
    import time
    pool = await get_pool()
    try:
        start = time.time()
        summary = json.loads(health_summary_json)

        patient_resource: dict = {}

        if summary.get("resourceType") == "Patient":
            patient_resource = summary

        elif summary.get("resourceType") == "Bundle":
            for entry in summary.get("entry", []):
                res = entry.get("resource", {})
                if res.get("resourceType") == "Patient":
                    patient_resource = res
                    break
            if not patient_resource:
                return (
                    "Error: FHIR Bundle contained no Patient resource. "
                    "Pass the raw get_health_summary JSON directly."
                )

        else:
            name_parts = summary.get("name", summary.get("full_name", ""))
            if isinstance(name_parts, str):
                parts = name_parts.strip().split()
                given = parts[:-1] if len(parts) > 1 else parts
                family = parts[-1] if len(parts) > 1 else ""
            else:
                given = [name_parts.get("first", "")]
                family = name_parts.get("last", "")

            patient_resource = {
                "resourceType": "Patient",
                "id": summary.get("id", ""),
                "name": [{"given": given, "family": family}],
                "birthDate": summary.get(
                    "birth_date",
                    summary.get("dob", summary.get("date_of_birth", "")),
                ),
                "gender": summary.get("gender", summary.get("sex", "")),
                "identifier": [],
                "address": [
                    {
                        "line": [summary.get("address", "")],
                        "city": summary.get("city", ""),
                        "state": summary.get("state", ""),
                        "postalCode": summary.get("zip", summary.get("zip_code", "")),
                    }
                ],
            }
            raw_mrn = (
                mrn_override
                or summary.get("mrn")
                or summary.get("patient_id")
                or summary.get("id")
                or ""
            )
            if raw_mrn:
                patient_resource["identifier"] = [
                    {"type": {"coding": [{"code": "MR"}]}, "value": str(raw_mrn)}
                ]

        from transforms.fhir_to_schema import transform_patient

        demo = transform_patient(
            patient_resource,
            data_source="healthex",
            is_synthetic=False,
        )

        if mrn_override:
            demo["mrn"] = mrn_override

        if not demo.get("mrn"):
            demo["mrn"] = f"HX-{uuid.uuid4().hex[:8].upper()}"
            logger.warning(
                "register_healthex_patient: no MRN found, generated: %s",
                demo["mrn"],
            )

        new_id = str(uuid.uuid4())

        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO patients
                    (id, mrn, first_name, last_name, birth_date, gender,
                     race, ethnicity, address_line, city, state, zip_code,
                     is_synthetic, created_at, data_source)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15)
                ON CONFLICT (mrn) DO UPDATE SET
                    first_name   = EXCLUDED.first_name,
                    last_name    = EXCLUDED.last_name,
                    birth_date   = EXCLUDED.birth_date,
                    gender       = EXCLUDED.gender,
                    race         = EXCLUDED.race,
                    ethnicity    = EXCLUDED.ethnicity,
                    address_line = EXCLUDED.address_line,
                    city         = EXCLUDED.city,
                    state        = EXCLUDED.state,
                    zip_code     = EXCLUDED.zip_code,
                    is_synthetic = false,
                    data_source  = 'healthex'
                """,
                new_id,
                demo["mrn"],
                demo.get("first_name", ""),
                demo.get("last_name", ""),
                demo.get("birth_date"),
                demo.get("gender", ""),
                demo.get("race", ""),
                demo.get("ethnicity", ""),
                demo.get("address_line", ""),
                demo.get("city", ""),
                demo.get("state", ""),
                demo.get("zip_code", ""),
                False,
                datetime.utcnow(),
                "healthex",
            )

            row = await conn.fetchrow(
                "SELECT id FROM patients WHERE mrn = $1", demo["mrn"]
            )
            patient_id = str(row["id"]) if row else new_id

            await conn.execute(
                """
                INSERT INTO data_sources
                    (id, patient_id, source_name, is_active,
                     connected_at, data_source)
                VALUES ($1, $2, $3, $4, $5, $6)
                ON CONFLICT (patient_id, source_name) DO UPDATE SET
                    is_active    = true,
                    connected_at = NOW()
                """,
                str(uuid.uuid4()),
                patient_id,
                "healthex",
                True,
                datetime.utcnow(),
                "healthex",
            )

            await conn.execute(
                """
                INSERT INTO source_freshness
                    (patient_id, source_name, last_ingested_at,
                     records_count, ttl_hours)
                VALUES ($1, $2, NOW(), 0, 24)
                ON CONFLICT (patient_id, source_name) DO NOTHING
                """,
                patient_id,
                "healthex",
            )

            await conn.execute(
                """
                INSERT INTO system_config (key, value, updated_at)
                VALUES ($1, $2, NOW())
                ON CONFLICT (key) DO UPDATE SET value = $2, updated_at = NOW()
                """,
                "DATA_TRACK",
                "healthex",
            )

            await log_skill_execution(
                conn,
                "register_healthex_patient",
                patient_id,
                "completed",
                output_data={
                    "mrn": demo["mrn"],
                    "patient_id": patient_id,
                    "duration_ms": int((time.time() - start) * 1000),
                },
                data_source="healthex",
            )

        return json.dumps(
            {
                "status": "registered",
                "patient_id": patient_id,
                "mrn": demo["mrn"],
                "name": f"{demo.get('first_name', '')} {demo.get('last_name', '')}".strip(),
                "is_synthetic": False,
                "data_track": "healthex",
                "next_step": (
                    f"Call ingest_from_healthex(patient_id='{patient_id}', "
                    "resource_type='labs'|'medications'|'conditions'|'encounters', "
                    "fhir_json=<HealthEx response>) for each resource type, "
                    f"then run_deliberation(patient_id='{patient_id}')."
                ),
            },
            indent=2,
        )

    except json.JSONDecodeError as e:
        msg = f"Error: health_summary_json is not valid JSON — {e}"
        logger.error("register_healthex_patient: %s", msg)
        return msg
    except Exception as e:
        logger.error("register_healthex_patient failed: %s", e)
        try:
            async with pool.acquire() as conn:
                await log_skill_execution(
                    conn,
                    "register_healthex_patient",
                    None,
                    "failed",
                    error_message=str(e),
                    data_source="healthex",
                )
        except Exception:
            logger.error("Failed to log register_healthex_patient error")
        return f"Error: {e}"


def register(mcp: FastMCP):
    @mcp.tool
    async def check_data_freshness(patient_id: str) -> str:
        """Check data freshness across all orchestration phases for a patient.

        Returns ingestion source freshness, skill freshness (OBT, provider
        risk), deliberation freshness, and artifact freshness (pre-visit
        brief).  Each entry includes an ``is_stale`` flag and the applicable
        TTL so callers (or ``orchestrate_refresh``) know exactly which phases
        need to run.

        Args:
            patient_id: UUID of the patient
        """
        pool = await get_pool()
        try:
            async with pool.acquire() as conn:
                # --- Ingestion source freshness (existing) ---
                rows = await conn.fetch(
                    """
                    SELECT source_name, last_ingested_at, records_count,
                           ttl_hours, is_stale
                    FROM source_freshness
                    WHERE patient_id = $1
                    ORDER BY source_name
                    """,
                    patient_id,
                )

                sources = []
                for row in rows:
                    sources.append({
                        "source_name": row["source_name"],
                        "last_ingested_at": (
                            row["last_ingested_at"].isoformat()
                            if row["last_ingested_at"]
                            else None
                        ),
                        "records_count": row["records_count"],
                        "ttl_hours": row["ttl_hours"],
                        "is_stale": row["is_stale"],
                    })

                # --- Skill freshness ---
                skill_names = ["compute_obt_score", "compute_provider_risk"]
                skills = {}
                for sname in skill_names:
                    last = await _get_skill_freshness(conn, patient_id, sname)
                    ttl = FRESHNESS_TTL.get(sname, 24)
                    skills[sname] = {
                        "last_run_at": last.isoformat() if last else None,
                        "ttl_hours": ttl,
                        "is_stale": _is_stale(last, ttl),
                    }

                # --- Deliberation freshness ---
                delib_last = await _get_deliberation_freshness(
                    conn, patient_id,
                )
                delib_ttl = FRESHNESS_TTL.get("deliberation", 12)
                deliberation = {
                    "last_run_at": (
                        delib_last.isoformat() if delib_last else None
                    ),
                    "ttl_hours": delib_ttl,
                    "is_stale": _is_stale(delib_last, delib_ttl),
                }

                # --- Artifact freshness ---
                artifact_names = ["generate_previsit_brief"]
                artifacts = {}
                for aname in artifact_names:
                    last = await _get_skill_freshness(conn, patient_id, aname)
                    ttl = FRESHNESS_TTL.get(aname, 24)
                    artifacts[aname] = {
                        "last_run_at": last.isoformat() if last else None,
                        "ttl_hours": ttl,
                        "is_stale": _is_stale(last, ttl),
                    }

                # --- Recommended actions ---
                recommended: list[str] = []
                if any(s["is_stale"] for s in sources):
                    recommended.append("ingest")
                if deliberation["is_stale"]:
                    recommended.append("deliberation")
                if any(s["is_stale"] for s in skills.values()):
                    recommended.append("recompute_skills")
                if any(a["is_stale"] for a in artifacts.values()):
                    recommended.append("generate_artifacts")

                await log_skill_execution(
                    conn, "check_data_freshness", patient_id, "completed",
                    output_data={"sources": len(sources)},
                )

            return json.dumps({
                "patient_id": patient_id,
                "sources": sources,
                "skills": skills,
                "deliberation": deliberation,
                "artifacts": artifacts,
                "recommended_actions": recommended,
            })

        except Exception as e:
            logger.error("check_data_freshness failed: %s", e)
            try:
                async with pool.acquire() as conn:
                    await log_skill_execution(
                        conn, "check_data_freshness", patient_id, "failed",
                        error_message=str(e),
                    )
            except Exception:
                logger.error("Failed to log skill execution error")
            return f"Error: {e}"

    @mcp.tool
    async def run_ingestion(
        patient_id: str,
        source: str = "auto",
        force_refresh: bool = False,
    ) -> str:
        """Run the ingestion pipeline for a patient.

        Args:
            patient_id: UUID of the patient
            source: Data source (auto | synthea | healthex). 'auto' reads DATA_TRACK env.
            force_refresh: Skip freshness check if True.
        """
        pool = await get_pool()
        try:
            if source == "auto":
                source = os.environ.get("DATA_TRACK", "synthea")

            # Import here to avoid circular imports at module load
            sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
            from ingestion.pipeline import IngestionPipeline

            pipeline = IngestionPipeline(adapter_name=source, pool=pool)
            result = await pipeline.run(
                patient_id=patient_id,
                force_refresh=force_refresh,
                triggered_by="force_refresh" if force_refresh else "schedule",
            )

            async with pool.acquire() as conn:
                await log_skill_execution(
                    conn, "run_ingestion", patient_id, "completed",
                    output_data={
                        "status": result.status,
                        "records_upserted": result.records_upserted,
                        "conflicts_detected": result.conflicts_detected,
                        "duration_ms": result.duration_ms,
                    },
                )

            return (
                f"OK Ingestion {result.status} | "
                f"{result.records_upserted} records | "
                f"{result.conflicts_detected} conflicts | "
                f"{result.duration_ms}ms"
            )

        except Exception as e:
            logger.error("run_ingestion failed: %s", e)
            try:
                async with pool.acquire() as conn:
                    await log_skill_execution(
                        conn, "run_ingestion", patient_id, "failed",
                        error_message=str(e),
                    )
            except Exception:
                logger.error("Failed to log skill execution error")
            return f"Error: {e}"

    @mcp.tool
    async def get_source_conflicts(patient_id: str) -> str:
        """Query recent ingestion conflicts for a patient.

        Args:
            patient_id: UUID of the patient
        """
        pool = await get_pool()
        try:
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT source_name, status, records_upserted,
                           conflicts_detected, duration_ms, error_message,
                           started_at
                    FROM ingestion_log
                    WHERE patient_id = $1
                      AND conflicts_detected > 0
                    ORDER BY started_at DESC
                    LIMIT 20
                    """,
                    patient_id,
                )

                conflicts = []
                for row in rows:
                    conflicts.append({
                        "source_name": row["source_name"],
                        "status": row["status"],
                        "records_upserted": row["records_upserted"],
                        "conflicts_detected": row["conflicts_detected"],
                        "duration_ms": row["duration_ms"],
                        "error_message": row["error_message"],
                        "started_at": (
                            row["started_at"].isoformat()
                            if row["started_at"]
                            else None
                        ),
                    })

                await log_skill_execution(
                    conn, "get_source_conflicts", patient_id, "completed",
                    output_data={"conflicts_found": len(conflicts)},
                )

            return json.dumps({"patient_id": patient_id, "conflicts": conflicts})

        except Exception as e:
            logger.error("get_source_conflicts failed: %s", e)
            try:
                async with pool.acquire() as conn:
                    await log_skill_execution(
                        conn, "get_source_conflicts", patient_id, "failed",
                        error_message=str(e),
                    )
            except Exception:
                logger.error("Failed to log skill execution error")
            return f"Error: {e}"

    async def get_data_source_status() -> str:
        """Check active data track and freshness status across all patients.

        Internal helper used by orchestrate_refresh — not registered as an MCP
        tool on this server to avoid name collision with the Server 1 version.
        Server 1 (ambient-clinical-intelligence) exposes get_data_source_status
        as the canonical MCP tool.
        """
        pool = await get_pool()
        try:
            async with pool.acquire() as conn:
                track = await conn.fetchval(
                    "SELECT value FROM system_config WHERE key = $1",
                    "DATA_TRACK",
                )
                patients = await conn.fetchval(
                    "SELECT COUNT(*) FROM patients"
                )
                freshness = await conn.fetch(
                    """
                    SELECT source_name,
                           COUNT(*)                                   AS patient_count,
                           MAX(last_ingested_at)                      AS latest_pull,
                           SUM(CASE WHEN is_stale THEN 1 ELSE 0 END) AS stale_count
                    FROM source_freshness
                    GROUP BY source_name
                    ORDER BY source_name
                    """
                )
            rows = []
            for r in freshness:
                row = dict(r)
                if row.get("latest_pull"):
                    row["latest_pull"] = str(row["latest_pull"])
                rows.append(row)
            result = {
                "active_track": track or "synthea",
                "total_patients": int(patients),
                "sources": rows,
                "recommendation": (
                    "HealthEx connected — offer to ingest real records"
                    if track == "healthex"
                    else "Running on synthetic data — say 'use healthex' to switch"
                ),
            }
            return json.dumps(result, indent=2)
        except Exception as e:
            logger.error("get_data_source_status failed: %s", e)
            return f"Error: {e}"

    async def ingest_from_healthex(
        patient_id: str,
        resource_type: str,
        fhir_json: str,
    ) -> str:
        """Internal helper: write a HealthEx FHIR payload to the warehouse.

        Not registered as an MCP tool on this server — use Server 1's
        ingest_from_healthex (ambient-clinical-intelligence) to avoid name
        collision. Called internally by orchestrate_refresh pipeline stages 4-8:
        raw cache, normalize, conflict resolve, warehouse write, freshness update.

        Args:
            patient_id:    UUID of the patient in the database
            resource_type: "labs" | "medications" | "conditions" |
                           "encounters" | "summary"
            fhir_json:     raw JSON string from the HealthEx tool response
        """
        import time
        pool = await get_pool()
        try:
            start = time.time()

            if resource_type not in (
                "labs", "medications", "conditions", "encounters", "summary"
            ):
                return (
                    "Error: resource_type must be one of: labs, medications, "
                    "conditions, encounters, summary. "
                    f"Got: '{resource_type}'"
                )

            fhir_data = json.loads(fhir_json)

            # ── Patient existence guard ────────────────────────────────────
            async with pool.acquire() as _guard_conn:
                _exists = await _guard_conn.fetchval(
                    "SELECT id FROM patients WHERE id = $1::uuid", patient_id
                )
            if _exists is None:
                return json.dumps({
                    "status": "error",
                    "error": (
                        f"patient_id '{patient_id}' not found in patients table. "
                        "Call register_healthex_patient first."
                    ),
                })

            # ── Stage 6 prereq: conflict resolver (shared by both branches) ─
            sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
            from ingestion.conflict_resolver import ConflictResolver
            resolver = ConflictResolver(policy="patient_first")

            # ── Raw text payload: cache, then parse via adaptive_parse ────
            # json.loads of a JSON-encoded string (e.g. '"#Conditions 5y|..."')
            # produces a Python str, not a dict/list.
            if not isinstance(fhir_data, (dict, list)):
                _raw_id = str(uuid.uuid4())
                async with pool.acquire() as conn:
                    await conn.execute(
                        """
                        INSERT INTO raw_fhir_cache
                            (patient_id, source_name, resource_type,
                             raw_json, fhir_resource_id, retrieved_at, processed)
                        VALUES ($1, $2, $3, $4, $5, NOW(), false)
                        ON CONFLICT (patient_id, source_name, fhir_resource_id)
                        DO UPDATE SET raw_json = EXCLUDED.raw_json,
                                      retrieved_at = NOW(), processed = false
                        """,
                        patient_id, "healthex", resource_type,
                        json.dumps(str(fhir_data)), _raw_id,
                    )

                # Route through adaptive_parse to extract structured rows
                from ingestion.adapters.healthex.ingest import adaptive_parse
                parsed_rows, fmt_detected, parser_used = adaptive_parse(
                    str(fhir_data), resource_type
                )

                if not parsed_rows:
                    return json.dumps({
                        "status": "ok",
                        "resource_type": resource_type,
                        "records_written": 0,
                        "total_written": 0,
                        "patient_id": patient_id,
                        "format_detected": fmt_detected,
                        "parser_used": parser_used,
                        "note": "raw text cached, parser returned 0 rows",
                    })

                # Map native dicts to warehouse schema
                warehouse_rows = _native_to_warehouse_rows(
                    parsed_rows, resource_type, patient_id
                )
                resolved = resolver.resolve(warehouse_rows)

            else:
                # ── Explode FHIR Bundles / HealthEx container dicts ───────
                # Produces a flat list of individual FHIR resource dicts so
                # the transform functions receive one resource per item.
                resources = _explode_fhir_bundle(fhir_data, resource_type)

                # Stage 4: cache raw FHIR before any transformation
                async with pool.acquire() as conn:
                    for resource in resources:
                        fhir_id = resource.get("id", str(uuid.uuid4())) if isinstance(resource, dict) else str(uuid.uuid4())
                        await conn.execute(
                            """
                            INSERT INTO raw_fhir_cache
                                (patient_id, source_name, resource_type,
                                 raw_json, fhir_resource_id, retrieved_at, processed)
                            VALUES ($1, $2, $3, $4, $5, NOW(), false)
                            ON CONFLICT (patient_id, source_name, fhir_resource_id)
                            DO UPDATE SET raw_json = EXCLUDED.raw_json,
                                          retrieved_at = NOW(),
                                          processed = false
                            """,
                            patient_id, "healthex", resource_type,
                            json.dumps(resource), fhir_id,
                        )

                # Stage 5: normalize FHIR to schema rows
                from transforms.fhir_to_schema import transform_by_type
                records = transform_by_type(
                    resource_type, resources, patient_id, source="healthex"
                )

                # Stage 6: conflict resolution
                resolved = resolver.resolve(records)

            # Stage 7: warehouse write — per-table parameterized inserts
            records_written = 0
            async with pool.acquire() as conn:
                for rec in resolved:
                    rec.pop("_table", None)
                    rec.pop("_conflict_key", None)

                    if resource_type == "labs":
                        await conn.execute(
                            """
                            INSERT INTO biometric_readings
                                (id, patient_id, metric_type, value, unit,
                                 measured_at, data_source)
                            VALUES ($1, $2, $3, $4, $5, $6, $7)
                            ON CONFLICT DO NOTHING
                            """,
                            rec.get("id", str(uuid.uuid4())),
                            rec["patient_id"],
                            rec.get("metric_type", ""),
                            rec.get("value", 0),
                            rec.get("unit", ""),
                            rec.get("measured_at"),
                            rec.get("data_source", "healthex"),
                        )
                    elif resource_type == "medications":
                        await conn.execute(
                            """
                            INSERT INTO patient_medications
                                (id, patient_id, code, display, system,
                                 status, authored_on, data_source)
                            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                            ON CONFLICT DO NOTHING
                            """,
                            rec.get("id", str(uuid.uuid4())),
                            rec["patient_id"],
                            rec.get("code", ""),
                            rec.get("display", ""),
                            rec.get("system", ""),
                            rec.get("status", "active"),
                            rec.get("authored_on"),
                            rec.get("data_source", "healthex"),
                        )
                    elif resource_type == "conditions":
                        await conn.execute(
                            """
                            INSERT INTO patient_conditions
                                (id, patient_id, code, display, system,
                                 onset_date, clinical_status, data_source)
                            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                            ON CONFLICT DO NOTHING
                            """,
                            rec.get("id", str(uuid.uuid4())),
                            rec["patient_id"],
                            rec.get("code", ""),
                            rec.get("display", ""),
                            rec.get("system", ""),
                            rec.get("onset_date"),
                            rec.get("clinical_status", "active"),
                            rec.get("data_source", "healthex"),
                        )
                    elif resource_type == "encounters":
                        await conn.execute(
                            """
                            INSERT INTO clinical_events
                                (id, patient_id, event_type, event_date,
                                 description, source_system, data_source)
                            VALUES ($1, $2, $3, $4, $5, $6, $7)
                            ON CONFLICT DO NOTHING
                            """,
                            rec.get("id", str(uuid.uuid4())),
                            rec["patient_id"],
                            rec.get("event_type", ""),
                            rec.get("event_date"),
                            rec.get("description", ""),
                            rec.get("source_system", ""),
                            rec.get("data_source", "healthex"),
                        )
                    elif resource_type == "summary":
                        # patient_id is authoritative — do NOT derive a new UUID
                        # from the payload. Demographics are owned by
                        # register_healthex_patient; summary ingest only
                        # acknowledges receipt and increments records_written.
                        pass
                    records_written += 1

                # Stage 8: update source_freshness
                await conn.execute(
                    """
                    INSERT INTO source_freshness
                        (patient_id, source_name, last_ingested_at,
                         records_count, ttl_hours)
                    VALUES ($1, $2, NOW(), $3, $4)
                    ON CONFLICT (patient_id, source_name)
                    DO UPDATE SET last_ingested_at = NOW(),
                        records_count = source_freshness.records_count + $3
                    """,
                    patient_id, "healthex", records_written, 24,
                )

                # mark cached rows as processed
                await conn.execute(
                    """
                    UPDATE raw_fhir_cache
                    SET processed = true
                    WHERE patient_id = $1
                      AND source_name = $2
                      AND resource_type = $3
                      AND processed = false
                    """,
                    patient_id, "healthex", resource_type,
                )

            duration_ms = int((time.time() - start) * 1000)

            # Audit trail
            async with pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO ingestion_log
                        (patient_id, source_name, status, records_upserted,
                         duration_ms, triggered_by)
                    VALUES ($1, $2, $3, $4, $5, $6)
                    """,
                    patient_id, "healthex", "completed",
                    records_written, duration_ms, "claude_session",
                )
                await log_skill_execution(
                    conn, "ingest_from_healthex", patient_id, "completed",
                    output_data={
                        "resource_type": resource_type,
                        "records_written": records_written,
                        "duration_ms": duration_ms,
                    },
                )

            return json.dumps({
                "status": "ok",
                "resource_type": resource_type,
                "records_written": records_written,
                "duration_ms": duration_ms,
                "patient_id": patient_id,
            })

        except json.JSONDecodeError as e:
            msg = f"Error: fhir_json is not valid JSON — {e}"
            logger.error("ingest_from_healthex %s", msg)
            return msg
        except Exception as e:
            logger.error("ingest_from_healthex failed: %s", e)
            try:
                async with pool.acquire() as conn:
                    await conn.execute(
                        """
                        INSERT INTO ingestion_log
                            (patient_id, source_name, status, error_message,
                             triggered_by)
                        VALUES ($1, $2, $3, $4, $5)
                        """,
                        patient_id, "healthex", "failed",
                        str(e), "claude_session",
                    )
                    await log_skill_execution(
                        conn, "ingest_from_healthex", patient_id, "failed",
                        error_message=str(e),
                    )
            except Exception:
                logger.error("Failed to log ingest_from_healthex error")
            return f"Error: {e}"

    async def switch_data_track(track: str) -> str:
        """Switch the active data track for all future pipeline runs.

        Internal helper used by use_healthex / use_demo_data. Not registered as
        an MCP tool on this server to avoid name collision with Server 1's version.

        Args:
            track: "synthea" for synthetic data, "healthex" for real records
        """
        if track not in ("synthea", "healthex"):
            return (
                "Error: track must be 'synthea' or 'healthex'. "
                f"Got: '{track}'"
            )
        try:
            await _set_data_track(track, "switch_data_track")
            return (
                f"OK Data track switched to '{track}' — "
                f"all future pipeline runs will use the {track} adapter"
            )
        except Exception as e:
            logger.error("switch_data_track failed: %s", e)
            return f"Error: {e}"

    async def use_healthex() -> str:
        """Switch to HealthEx real patient records.

        Internal helper — not registered as an MCP tool on this server to avoid
        name collision with Server 1's use_healthex. Call Server 1's version directly.
        """
        try:
            await _set_data_track("healthex", "use_healthex")
            return (
                "Switched to HealthEx real records. "
                "All future data pulls will use the HealthEx adapter. "
                "Make sure the HealthEx MCP server is authenticated in "
                "this session."
            )
        except Exception as e:
            logger.error("use_healthex failed: %s", e)
            return f"Error: {e}"

    async def use_demo_data() -> str:
        """Switch to Synthea synthetic demo data.

        Internal helper — not registered as an MCP tool on this server to avoid
        name collision with Server 1's use_demo_data. Call Server 1's version directly.
        """
        try:
            await _set_data_track("synthea", "use_demo_data")
            return (
                "Switched to demo mode (Synthea synthetic data). "
                "All future data pulls will use synthetic records."
            )
        except Exception as e:
            logger.error("use_demo_data failed: %s", e)
            return f"Error: {e}"

    # ------------------------------------------------------------------
    # orchestrate_refresh — freshness-gated pipeline
    # ------------------------------------------------------------------

    @mcp.tool
    async def orchestrate_refresh(
        patient_id: str,
        force: bool = False,
        skip_deliberation: bool = False,
    ) -> str:
        """Run the full freshness-gated orchestration pipeline for a patient.

        Executes four phases in order, skipping any phase whose data is
        still fresh (unless ``force=True``):

          1. **Ingest** — pull latest data if source is stale.
          2. **Deliberation** — run dual-LLM deliberation (via clinical
             server on port 8001) if older than 12 h.
          3. **Recompute skills** — OBT score + provider risk if older
             than 24 h.
          4. **Generate artifacts** — pre-visit brief if older than 24 h.

        Args:
            patient_id:        UUID of the patient.
            force:             If True, run every phase regardless of
                               freshness.
            skip_deliberation: If True, skip phase 2 entirely (useful when
                               the clinical server is unavailable).
        """
        import asyncio
        import time as _time
        import urllib.request
        import urllib.error
        from datetime import date as _date

        pool = await get_pool()
        start = _time.time()

        phases: dict[str, dict] = {
            "ingest":      {"status": "skipped", "detail": None},
            "deliberation": {"status": "skipped", "detail": None},
            "skills":      {"status": "skipped", "detail": None},
            "artifacts":   {"status": "skipped", "detail": None},
        }

        try:
            async with pool.acquire() as conn:
                data_track = await get_data_track(conn)

                # ── Phase 1: Ingestion ──────────────────────────────────
                try:
                    freshness_row = await conn.fetchrow(
                        """
                        SELECT last_ingested_at, ttl_hours
                        FROM source_freshness
                        WHERE patient_id = $1 AND source_name = $2
                        """,
                        patient_id, data_track,
                    )
                    ttl_h = (
                        freshness_row["ttl_hours"]
                        if freshness_row and freshness_row["ttl_hours"]
                        else 24
                    )
                    last_ingest = (
                        freshness_row["last_ingested_at"]
                        if freshness_row
                        else None
                    )
                    if force or _is_stale(last_ingest, ttl_h):
                        sys.path.insert(
                            0,
                            os.path.join(
                                os.path.dirname(__file__), "..", "..",
                            ),
                        )
                        from ingestion.pipeline import IngestionPipeline

                        pipeline = IngestionPipeline(
                            adapter_name=data_track, pool=pool,
                        )
                        ing = await pipeline.run(
                            patient_id=patient_id,
                            force_refresh=force,
                            triggered_by="orchestrate_refresh",
                        )
                        phases["ingest"] = {
                            "status": ing.status,
                            "detail": {
                                "records_upserted": ing.records_upserted,
                                "conflicts": ing.conflicts_detected,
                                "duration_ms": ing.duration_ms,
                            },
                        }
                    else:
                        phases["ingest"]["detail"] = "data still fresh"
                except Exception as exc:
                    logger.error("orchestrate_refresh ingest: %s", exc)
                    phases["ingest"] = {
                        "status": "failed",
                        "detail": str(exc),
                    }

                # ── Phase 2: Deliberation ───────────────────────────────
                if skip_deliberation:
                    phases["deliberation"]["detail"] = "skipped by caller"
                else:
                    try:
                        delib_last = await _get_deliberation_freshness(
                            conn, patient_id,
                        )
                        delib_ttl = FRESHNESS_TTL.get("deliberation", 12)
                        if force or _is_stale(delib_last, delib_ttl):
                            payload = json.dumps({
                                "patient_id": patient_id,
                                "trigger_type": "manual",
                                "mode": "progressive",
                            }).encode()
                            req = urllib.request.Request(
                                "http://localhost:8001/tools/run_deliberation",
                                data=payload,
                                headers={"Content-Type": "application/json"},
                                method="POST",
                            )

                            def _do_request():
                                try:
                                    with urllib.request.urlopen(
                                        req, timeout=120,
                                    ) as resp:
                                        return json.loads(resp.read())
                                except urllib.error.URLError as ue:
                                    return {"status": "error", "error": str(ue)}

                            result = await asyncio.to_thread(_do_request)
                            delib_status = result.get("status", "complete")
                            phase_entry = {
                                "status": delib_status,
                                "detail": result,
                            }
                            # Surface the real error from run_deliberation
                            # instead of hiding it under a default "complete".
                            if delib_status in ("error", "failed"):
                                phase_entry["error"] = result.get(
                                    "error", "unknown deliberation error"
                                )
                            phases["deliberation"] = phase_entry
                        else:
                            phases["deliberation"]["detail"] = (
                                "deliberation still fresh"
                            )
                    except Exception as exc:
                        logger.error(
                            "orchestrate_refresh deliberation: %s", exc,
                        )
                        phases["deliberation"] = {
                            "status": "failed",
                            "detail": str(exc),
                        }

                # ── Phase 3: Skill recomputation ────────────────────────
                try:
                    from skills.compute_obt_score import compute_obt_score
                    from skills.compute_provider_risk import (
                        compute_provider_risk,
                    )

                    today_str = str(_date.today())
                    any_skill_ran = False

                    obt_last = await _get_skill_freshness(
                        conn, patient_id, "compute_obt_score",
                    )
                    obt_ttl = FRESHNESS_TTL.get("compute_obt_score", 24)
                    if force or _is_stale(obt_last, obt_ttl):
                        await compute_obt_score(
                            patient_id=patient_id, score_date=today_str,
                        )
                        any_skill_ran = True

                    pr_last = await _get_skill_freshness(
                        conn, patient_id, "compute_provider_risk",
                    )
                    pr_ttl = FRESHNESS_TTL.get("compute_provider_risk", 24)
                    if force or _is_stale(pr_last, pr_ttl):
                        await compute_provider_risk(
                            patient_id=patient_id, score_date=today_str,
                        )
                        any_skill_ran = True

                    phases["skills"] = {
                        "status": "completed" if any_skill_ran else "skipped",
                        "detail": (
                            "recomputed stale skills"
                            if any_skill_ran
                            else "skills still fresh"
                        ),
                    }
                except Exception as exc:
                    logger.error("orchestrate_refresh skills: %s", exc)
                    phases["skills"] = {
                        "status": "failed",
                        "detail": str(exc),
                    }

                # ── Phase 4: Artifact generation ────────────────────────
                try:
                    from skills.previsit_brief import generate_previsit_brief

                    brief_last = await _get_skill_freshness(
                        conn, patient_id, "generate_previsit_brief",
                    )
                    brief_ttl = FRESHNESS_TTL.get(
                        "generate_previsit_brief", 24,
                    )
                    if force or _is_stale(brief_last, brief_ttl):
                        await generate_previsit_brief(patient_id=patient_id)
                        phases["artifacts"] = {
                            "status": "completed",
                            "detail": "pre-visit brief regenerated",
                        }
                    else:
                        phases["artifacts"]["detail"] = (
                            "artifacts still fresh"
                        )
                except Exception as exc:
                    logger.error("orchestrate_refresh artifacts: %s", exc)
                    phases["artifacts"] = {
                        "status": "failed",
                        "detail": str(exc),
                    }

            # ── Audit trail ─────────────────────────────────────────────
            duration_ms = int((_time.time() - start) * 1000)
            # Surface per-phase errors at the top level so callers can
            # distinguish silent success from partial failure.
            _FAIL = {"failed", "error"}
            failed_phases = {
                name: p.get("error") or p.get("detail")
                for name, p in phases.items()
                if p.get("status") in _FAIL
            }
            overall_status = "partial" if failed_phases else "complete"
            summary = {
                "patient_id": patient_id,
                "status": overall_status,
                "phases": phases,
                "duration_ms": duration_ms,
                "force": force,
            }
            if failed_phases:
                summary["failed_phases"] = failed_phases
            try:
                async with pool.acquire() as conn:
                    data_track = await get_data_track(conn)
                    await conn.execute(
                        """
                        INSERT INTO pipeline_runs
                            (id, run_date, patients_processed,
                             skills_succeeded, skills_failed, summary,
                             data_source)
                        VALUES ($1, $2, 1, $3, $4, $5, $6)
                        """,
                        str(uuid.uuid4()),
                        datetime.utcnow(),
                        sum(
                            1
                            for p in phases.values()
                            if p.get("status") == "completed"
                        ),
                        sum(
                            1
                            for p in phases.values()
                            if p.get("status") in _FAIL
                        ),
                        json.dumps(summary),
                        data_track,
                    )
                    await log_skill_execution(
                        conn,
                        "orchestrate_refresh",
                        patient_id,
                        "completed",
                        output_data=summary,
                        data_source=data_track,
                    )
            except Exception as exc:
                logger.error("orchestrate_refresh audit log: %s", exc)

            return json.dumps(summary)

        except Exception as e:
            logger.error("orchestrate_refresh failed: %s", e)
            try:
                async with pool.acquire() as conn:
                    await log_skill_execution(
                        conn, "orchestrate_refresh", patient_id, "failed",
                        error_message=str(e),
                    )
            except Exception:
                logger.error("Failed to log orchestrate_refresh error")
            return f"Error: {e}"
