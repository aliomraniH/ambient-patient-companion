"""
executor.py — Phase 2 worker for the two-phase ingestion architecture.

Reads pending ingestion plans from the ingestion_plans table, fetches
the raw blob from raw_fhir_cache, routes to the appropriate parser via
adaptive_parse(), and writes structured rows through the existing
_normalize_to_fhir() → _transform_and_write() pipeline.

All functions are async (asyncpg).
"""

import json
import logging
import time
import uuid as _uuid_mod

log = logging.getLogger(__name__)


async def execute_pending_plans(
    pool,
    patient_id: str = None,
    plan_id: str = None,
    limit: int = 10,
) -> dict:
    """
    Execute pending extraction plans. Writes structured rows to the warehouse.

    Call with plan_id to execute one specific plan.
    Call with patient_id to execute all pending plans for that patient.
    Call with neither to process the oldest N pending plans globally.

    Args:
        pool: asyncpg connection pool
        patient_id: UUID string of the patient
        plan_id: optional specific plan ID
        limit: max plans to execute (default 10)

    Returns:
        dict with plans_executed, total_rows_written, results list
    """
    async with pool.acquire() as conn:
        if plan_id:
            plans = await conn.fetch(
                "SELECT * FROM ingestion_plans WHERE id = $1::uuid AND status = 'pending'",
                plan_id,
            )
        elif patient_id:
            plans = await conn.fetch(
                """SELECT * FROM ingestion_plans
                   WHERE patient_id = $1::uuid
                     AND status IN ('pending', 'failed')
                   ORDER BY planned_at ASC LIMIT $2""",
                patient_id, limit,
            )
        else:
            plans = await conn.fetch(
                """SELECT * FROM ingestion_plans
                   WHERE status = 'pending'
                   ORDER BY planned_at ASC LIMIT $1""",
                limit,
            )

    if not plans:
        return {"plans_executed": 0, "total_rows_written": 0, "message": "No pending plans found", "results": []}

    results = []
    for plan in plans:
        result = await _execute_one_plan(pool, dict(plan))
        results.append(result)

    total_rows = sum(r.get("rows_written", 0) for r in results)
    return {
        "plans_executed": len(results),
        "total_rows_written": total_rows,
        "results": results,
    }


async def _execute_one_plan(pool, plan: dict) -> dict:
    """Execute one ingestion plan: fetch raw → parse → write → update plan."""
    import sys
    from pathlib import Path

    plan_id = str(plan["id"])
    patient_id = str(plan["patient_id"])
    cache_id = plan["cache_id"]
    resource_type = plan["resource_type"]

    # Mark as running
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE ingestion_plans SET status = 'running', executed_at = NOW() WHERE id = $1::uuid",
            plan_id,
        )

    start_ms = int(time.time() * 1000)

    try:
        # Fetch raw text from cache
        async with pool.acquire() as conn:
            cache_row = await conn.fetchrow(
                """SELECT raw_text, raw_json
                   FROM raw_fhir_cache
                   WHERE fhir_resource_id = $1 AND patient_id = $2::uuid""",
                cache_id, patient_id,
            )
            if cache_row is None:
                # Try by id directly
                cache_row = await conn.fetchrow(
                    "SELECT raw_text, raw_json FROM raw_fhir_cache WHERE id::text = $1",
                    cache_id,
                )

        if cache_row is None:
            raise ValueError(f"cache_id {cache_id} not found in raw_fhir_cache")

        # Get raw text — prefer raw_text, fall back to raw_json
        raw = cache_row.get("raw_text")
        if not raw:
            raw_json = cache_row.get("raw_json")
            if raw_json is not None:
                raw = json.dumps(raw_json) if not isinstance(raw_json, str) else raw_json
            else:
                raw = ""

        if not raw:
            raise ValueError("Empty raw text in cache")

        # Import the adaptive parser
        try:
            from ingestion.adapters.healthex.ingest import adaptive_parse
        except ImportError:
            _parent = str(Path(__file__).resolve().parent.parent.parent.parent)
            if _parent not in sys.path:
                sys.path.insert(0, _parent)
            from ingestion.adapters.healthex.ingest import adaptive_parse

        # Parse
        native_items, format_detected, parser_used = adaptive_parse(raw, resource_type)

        if not native_items:
            async with pool.acquire() as conn:
                await conn.execute(
                    """UPDATE ingestion_plans
                       SET status = 'complete', rows_written = 0, rows_verified = 0,
                           extraction_time_ms = $1
                       WHERE id = $2::uuid""",
                    int(time.time() * 1000) - start_ms, plan_id,
                )
            return {"plan_id": plan_id, "status": "complete", "rows_written": 0, "parser_used": parser_used}

        # Build transfer plan (size-aware chunking, assigns batch_id + chunk IDs)
        from .transfer_planner import plan_transfer
        from .traced_writer import execute_transfer_plan_async

        payload_bytes = len(raw.encode("utf-8", errors="replace"))
        transfer_plan = plan_transfer(
            patient_id=patient_id,
            resource_type=resource_type,
            records=native_items,
            payload_bytes=payload_bytes,
            format_detected=format_detected,
            source="healthex",
        )

        # Execute with per-record audit trail written to transfer_log
        tw_result = await execute_transfer_plan_async(pool, transfer_plan, patient_id)

        rows_written = tw_result.get("records_written", 0)
        rows_verified = tw_result.get("records_verified", 0)
        duration_ms = int(time.time() * 1000) - start_ms
        status = "complete" if rows_written > 0 else "failed"

        async with pool.acquire() as conn:
            await conn.execute(
                """UPDATE ingestion_plans
                   SET status = $1, rows_written = $2, rows_verified = $3,
                       extraction_time_ms = $4
                   WHERE id = $5::uuid""",
                status, rows_written, rows_verified, duration_ms, plan_id,
            )

        return {
            "plan_id": plan_id,
            "resource_type": resource_type,
            "status": status,
            "rows_written": rows_written,
            "rows_verified": rows_verified,
            "format_detected": format_detected,
            "parser_used": parser_used,
            "duration_ms": duration_ms,
            "batch_id": tw_result.get("batch_id", ""),
            "strategy": tw_result.get("strategy", ""),
        }

    except Exception as e:
        log.error("execute_one_plan failed for plan %s: %s", plan_id, e, exc_info=True)
        async with pool.acquire() as conn:
            await conn.execute(
                """UPDATE ingestion_plans
                   SET status = 'failed', error_message = $1,
                       retry_count = retry_count + 1
                   WHERE id = $2::uuid""",
                str(e)[:500], plan_id,
            )
        return {"plan_id": plan_id, "status": "failed", "error": str(e)}


# ---------------------------------------------------------------------------
# Local copies of the normaliser + writer helpers from mcp_server.py
# These avoid a circular import — they replicate the exact same logic.
# ---------------------------------------------------------------------------

def _normalize_to_fhir(resource_type: str, raw_resources: list[dict]) -> list[dict]:
    """Ensure items are proper FHIR resources; convert from native if needed."""
    if not raw_resources:
        return []

    fhir_type_map = {
        "conditions": "Condition",
        "medications": "MedicationRequest",
        "labs": "Observation",
        "encounters": "Encounter",
    }
    expected = fhir_type_map.get(resource_type, "")
    sample = raw_resources[0]

    if expected and sample.get("resourceType") == expected:
        return raw_resources

    converters = {
        "conditions": _native_to_fhir_conditions,
        "medications": _native_to_fhir_medications,
        "labs": _native_to_fhir_observations,
        "encounters": _native_to_fhir_encounters,
    }
    fn = converters.get(resource_type)
    return fn(raw_resources) if fn else raw_resources


def _native_to_fhir_conditions(items: list[dict]) -> list[dict]:
    out = []
    for item in items:
        if not isinstance(item, dict):
            continue
        code = item.get("icd10") or item.get("code") or ""
        display = item.get("name") or item.get("display") or item.get("description") or ""
        status = item.get("status") or "active"
        onset = item.get("onset_date") or item.get("onsetDate") or item.get("diagnosed_date") or item.get("onset") or ""
        out.append({
            "resourceType": "Condition",
            "code": {"coding": [{"code": code, "display": display, "system": "http://snomed.info/sct"}]},
            "clinicalStatus": {"coding": [{"code": status}]},
            "onsetDateTime": onset,
        })
    return out


def _native_to_fhir_medications(items: list[dict]) -> list[dict]:
    out = []
    for item in items:
        if not isinstance(item, dict):
            continue
        code = item.get("rxnorm") or item.get("code") or ""
        display = item.get("name") or item.get("display") or item.get("drug_name") or ""
        status = item.get("status") or "active"
        authored = item.get("start_date") or item.get("authoredOn") or item.get("prescribed_date") or ""
        out.append({
            "resourceType": "MedicationRequest",
            "medicationCodeableConcept": {"coding": [{"code": code, "display": display}]},
            "status": status,
            "authoredOn": authored,
        })
    return out


def _native_to_fhir_observations(items: list[dict]) -> list[dict]:
    """Convert native lab items to FHIR Observations.

    IMPORTANT: This version does NOT drop non-numeric values. If the value
    cannot be parsed as a float, it is stored as 0.0 with the original text
    preserved in the display field. This fixes the "1 blob" bug where
    labs with string values (e.g. "Positive", "Normal") were silently dropped.
    """
    out = []
    for item in items:
        if not isinstance(item, dict):
            continue
        code = item.get("loinc") or item.get("code") or ""
        display = item.get("name") or item.get("display") or item.get("test_name") or ""
        unit = item.get("unit") or item.get("units") or ""
        date = (item.get("date") or item.get("effectiveDateTime")
                or item.get("collected_date") or item.get("resulted_date") or "")
        raw_val = item.get("value") or item.get("result") or item.get("numeric_value")
        if raw_val is None:
            raw_val = ""

        # Try numeric conversion; fall back to 0.0 with original in unit
        try:
            numeric = float(str(raw_val).split()[0])
        except (ValueError, TypeError, IndexError):
            numeric = 0.0
            if raw_val:
                unit = f"{raw_val} ({unit})" if unit else str(raw_val)

        out.append({
            "resourceType": "Observation",
            "code": {"coding": [{"code": code, "display": display}]},
            "valueQuantity": {"value": numeric, "unit": unit},
            "effectiveDateTime": date,
        })
    return out


def _native_to_fhir_encounters(items: list[dict]) -> list[dict]:
    out = []
    for item in items:
        if not isinstance(item, dict):
            continue
        enc_type = item.get("type") or item.get("encounter_type") or item.get("visit_type") or "encounter"
        date = (item.get("date") or item.get("start_date")
                or item.get("encounter_date") or item.get("visit_date") or "")
        out.append({
            "resourceType": "Encounter",
            "type": [{"coding": [{"display": enc_type}]}],
            "period": {"start": date},
        })
    return out


async def _transform_and_write_rows(
    conn,
    resource_type: str,
    fhir_resources: list[dict],
    patient_id: str,
    transform_conditions,
    transform_medications,
    transform_clinical_observations,
    transform_encounters,
) -> int:
    """Transform FHIR resources to DB records and write them. Returns count."""
    if not fhir_resources:
        return 0

    transform_fn_map = {
        "conditions": lambda r: transform_conditions(r, patient_id, "healthex"),
        "medications": lambda r: transform_medications(r, patient_id, "healthex"),
        "labs": lambda r: transform_clinical_observations(r, patient_id, "healthex"),
        "encounters": lambda r: transform_encounters(r, patient_id, "healthex"),
    }
    fn = transform_fn_map.get(resource_type)
    if not fn:
        return 0

    records = fn(fhir_resources)
    writer = _WRITER_MAP.get(resource_type)
    if not writer:
        return 0

    return await writer(conn, records)


async def _write_condition_rows(conn, records: list[dict]) -> int:
    n = 0
    for rec in records:
        try:
            await conn.execute(
                """INSERT INTO patient_conditions
                       (id, patient_id, code, display, onset_date,
                        clinical_status, data_source)
                   VALUES ($1,$2,$3,$4,$5,$6,$7)
                   ON CONFLICT DO NOTHING""",
                rec.get("id", str(_uuid_mod.uuid4())),
                rec["patient_id"], rec.get("code", ""),
                rec.get("display", ""), rec.get("onset_date"),
                rec.get("clinical_status", "active"), "healthex",
            )
            n += 1
        except Exception as e:
            log.warning("write condition row failed: %s", e)
    return n


async def _write_medication_rows(conn, records: list[dict]) -> int:
    n = 0
    for rec in records:
        try:
            await conn.execute(
                """INSERT INTO patient_medications
                       (id, patient_id, code, display, status,
                        authored_on, data_source)
                   VALUES ($1,$2,$3,$4,$5,$6,$7)
                   ON CONFLICT DO NOTHING""",
                rec.get("id", str(_uuid_mod.uuid4())),
                rec["patient_id"], rec.get("code", ""),
                rec.get("display", ""), rec.get("status", "active"),
                rec.get("authored_on"), "healthex",
            )
            n += 1
        except Exception as e:
            log.warning("write medication row failed: %s", e)
    return n


async def _write_lab_rows(conn, records: list[dict]) -> int:
    n = 0
    for rec in records:
        try:
            await conn.execute(
                """INSERT INTO biometric_readings
                       (id, patient_id, metric_type, value, unit,
                        measured_at, data_source)
                   VALUES ($1,$2,$3,$4,$5,$6,$7)
                   ON CONFLICT DO NOTHING""",
                rec.get("id", str(_uuid_mod.uuid4())),
                rec["patient_id"], rec.get("metric_type", ""),
                rec.get("value"), rec.get("unit", ""),
                rec.get("measured_at"), "healthex",
            )
            n += 1
        except Exception as e:
            log.warning("write lab row failed: %s", e)
    return n


async def _write_encounter_rows(conn, records: list[dict]) -> int:
    n = 0
    for rec in records:
        try:
            await conn.execute(
                """INSERT INTO clinical_events
                       (id, patient_id, event_type, event_date,
                        description, data_source)
                   VALUES ($1,$2,$3,$4,$5,$6)
                   ON CONFLICT DO NOTHING""",
                rec.get("id", str(_uuid_mod.uuid4())),
                rec["patient_id"],
                rec.get("event_type", "encounter"),
                rec.get("event_date"),
                rec.get("description", ""), "healthex",
            )
            n += 1
        except Exception as e:
            log.warning("write encounter row failed: %s", e)
    return n


_WRITER_MAP = {
    "conditions": _write_condition_rows,
    "medications": _write_medication_rows,
    "labs": _write_lab_rows,
    "encounters": _write_encounter_rows,
}
