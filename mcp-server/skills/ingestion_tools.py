"""Skill: ingestion tools — freshness checks, ingestion triggers, conflict queries."""

from __future__ import annotations

import json
import logging
import os
import sys
import uuid
from datetime import datetime

from fastmcp import FastMCP

from db.connection import get_pool
from skills.base import log_skill_execution

logging.basicConfig(level=logging.INFO, stream=sys.stderr)
logger = logging.getLogger(__name__)


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
        """Check data freshness status for all sources of a patient.

        Args:
            patient_id: UUID of the patient
        """
        pool = await get_pool()
        try:
            async with pool.acquire() as conn:
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

                await log_skill_execution(
                    conn, "check_data_freshness", patient_id, "completed",
                    output_data={"sources": len(sources)},
                )

            return json.dumps({"patient_id": patient_id, "sources": sources})

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

    @mcp.tool
    async def get_data_source_status() -> str:
        """Check active data track and freshness status across all patients.

        Call this at the start of every Claude session to decide whether
        to use HealthEx real records or Synthea synthetic data.
        Returns active_track, patient count, and per-source freshness rows.
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

    mcp.tool(register_healthex_patient)

    @mcp.tool
    async def ingest_from_healthex(
        patient_id: str,
        resource_type: str,
        fhir_json: str,
    ) -> str:
        """Accept a HealthEx MCP tool response and write it to the warehouse.

        Claude calls the HealthEx tools (Get Lab Results, Get Medications,
        Get Conditions, Get Encounters, General Health Summary) in the
        session where HealthEx is authenticated, then passes each response
        here. This tool runs pipeline stages 4-8 only — raw cache,
        normalize, conflict resolve, warehouse write, freshness update.

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
            resources = (
                fhir_data if isinstance(fhir_data, list) else [fhir_data]
            )

            # Stage 4: cache raw FHIR before any transformation
            async with pool.acquire() as conn:
                for resource in resources:
                    fhir_id = resource.get("id", str(uuid.uuid4()))
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
            sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
            from ingestion.conflict_resolver import ConflictResolver
            resolver = ConflictResolver(policy="patient_first")
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
                        await conn.execute(
                            """
                            INSERT INTO patients
                                (id, mrn, first_name, last_name, birth_date,
                                 gender, race, ethnicity, address_line, city,
                                 state, zip_code, is_synthetic, data_source)
                            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10,
                                    $11, $12, $13, $14)
                            ON CONFLICT (mrn) DO UPDATE SET
                                first_name = EXCLUDED.first_name,
                                last_name = EXCLUDED.last_name,
                                birth_date = EXCLUDED.birth_date,
                                gender = EXCLUDED.gender,
                                race = EXCLUDED.race,
                                ethnicity = EXCLUDED.ethnicity,
                                data_source = EXCLUDED.data_source
                            """,
                            rec.get("id", str(uuid.uuid4())),
                            rec.get("mrn", ""),
                            rec.get("first_name", ""),
                            rec.get("last_name", ""),
                            rec.get("birth_date"),
                            rec.get("gender", ""),
                            rec.get("race", ""),
                            rec.get("ethnicity", ""),
                            rec.get("address_line", ""),
                            rec.get("city", ""),
                            rec.get("state", ""),
                            rec.get("zip_code", ""),
                            rec.get("is_synthetic", False),
                            rec.get("data_source", "healthex"),
                        )
                        # Retrieve the UUID that survived ON CONFLICT (mrn)
                        # so callers that skipped register_healthex_patient
                        # can recover the correct patient_id from the return value.
                        _summary_row = await conn.fetchrow(
                            "SELECT id FROM patients WHERE mrn = $1",
                            rec.get("mrn", ""),
                        )
                        if _summary_row:
                            patient_id = str(_summary_row["id"])
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

    @mcp.tool
    async def switch_data_track(track: str) -> str:
        """Switch the active data track for all future pipeline runs.

        Persists the choice to system_config so the orchestrator and
        seed pipeline pick up the correct adapter on the next run.

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

    @mcp.tool
    async def use_healthex() -> str:
        """Switch to HealthEx real patient records.

        Call this when the user wants to use real clinical data from
        HealthEx instead of synthetic demo data. Trigger phrases:
        "use healthex", "switch to real data", "connect healthex",
        "use real records", "switch to healthex".

        Note: HealthEx requires a Claude web session with the HealthEx
        MCP server authenticated. It will not work in Claude Code.
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

    @mcp.tool
    async def use_demo_data() -> str:
        """Switch to Synthea synthetic demo data.

        Call this when the user wants to use synthetic demo data
        instead of real records. Trigger phrases: "use demo data",
        "switch to synthea", "use synthetic data", "go back to demo",
        "use fake data", "switch to demo mode".
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
