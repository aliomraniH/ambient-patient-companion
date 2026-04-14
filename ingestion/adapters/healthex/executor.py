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
import sys
import time
import uuid as _uuid_mod
from pathlib import Path

log = logging.getLogger(__name__)

# Allow importing behavioral library modules that live under mcp-server/skills/.
# The ingestion server runs from the repo root; the skills modules are shared
# Python libraries (they take a `conn` parameter and do not import the
# mcp-server-specific `db.connection` module).
_MCP_SKILLS_ROOT = Path(__file__).resolve().parents[3] / "mcp-server"
if str(_MCP_SKILLS_ROOT) not in sys.path:
    sys.path.insert(0, str(_MCP_SKILLS_ROOT))


async def _post_process_notes_for_atoms(pool, patient_id: str, batch_start_ts) -> int:
    """Extract behavioral-signal atoms from clinical_notes just written in this
    batch. Refreshes the pressure-score view and runs the gap detector when
    atoms are inserted. Returns total atoms inserted.

    Best-effort: never raises. Failures are logged without PHI.
    """
    try:
        from skills.behavioral_atom_extractor import (
            extract_atoms_from_note, insert_atoms,
        )
        from skills.behavioral_atom_pressure import refresh_pressure_scores
        from skills.behavioral_gap_detector import run_gap_detector_for_patient
    except Exception as e:
        log.warning("behavioral libs unavailable: %s", type(e).__name__)
        return 0

    total_inserted = 0
    try:
        async with pool.acquire() as conn:
            note_rows = await conn.fetch(
                """SELECT id, note_text, note_date
                     FROM clinical_notes
                    WHERE patient_id = $1::uuid
                      AND ingested_at >= $2
                      AND note_text IS NOT NULL
                      AND char_length(note_text) > 50
                    ORDER BY ingested_at ASC""",
                patient_id, batch_start_ts,
            )
            for row in note_rows:
                note_date = row["note_date"]
                if note_date is None:
                    continue
                # clinical_notes.note_date is TIMESTAMPTZ — take the date part.
                from datetime import datetime as _dt
                if isinstance(note_date, _dt):
                    clinical_d = note_date.date()
                else:
                    clinical_d = note_date
                atoms = await extract_atoms_from_note(
                    note_text=row["note_text"],
                    note_date=clinical_d,
                    source_note_id=str(row["id"]),
                    patient_id=patient_id,
                )
                if atoms:
                    total_inserted += await insert_atoms(conn, atoms)

            if total_inserted > 0:
                await refresh_pressure_scores(conn)
                try:
                    # v2: returns list[dict] of newly opened domain gaps.
                    gaps = await run_gap_detector_for_patient(conn, patient_id)
                    if gaps:
                        log.info("gap detector opened %d domain gap(s)", len(gaps))
                except Exception as e:
                    log.warning("gap detector failed: %s", type(e).__name__)
    except Exception as e:
        log.warning("atom post-processing failed: %s", type(e).__name__)
    return total_inserted


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

    from datetime import datetime, timezone
    start_ms = int(time.time() * 1000)
    start_dt = datetime.now(timezone.utc)

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

        # Get raw text — prefer raw_text, fall back to raw_json.
        # BUG 3: raw_json is JSONB — asyncpg returns it as a string (no codec
        # registered). If a codec ever returns a dict, json.dumps() serializes
        # it back; either way, strip NUL bytes so downstream json.loads() in
        # _extract_routable_resources() cannot crash on the stored document.
        raw = cache_row.get("raw_text")
        if not raw:
            raw_json = cache_row.get("raw_json")
            if raw_json is not None:
                raw = json.dumps(raw_json) if not isinstance(raw_json, str) else raw_json
            else:
                raw = ""
        if isinstance(raw, str) and "\x00" in raw:
            raw = raw.replace("\x00", "")

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

        # Supplemental: route any Binary/DocumentReference/Observation resources
        # through the content router → clinical_notes / media_references
        media_result = {}
        try:
            from .content_router import route_and_write_resources
            fhir_resources = _extract_routable_resources(raw)
            if fhir_resources:
                async with pool.acquire() as conn:
                    media_result = await route_and_write_resources(
                        conn, fhir_resources, patient_id
                    )
                notes_n = media_result.get("notes_written", 0)
                refs_n  = media_result.get("refs_written", 0)
                if notes_n or refs_n:
                    rows_written += notes_n + refs_n
                    status = "complete"
        except Exception as e:
            log.warning("content router supplemental step failed: %s", e)

        # Supplemental: extract behavioral signal atoms from any clinical_notes
        # that were just written for this patient. Best-effort — never fails
        # the ingestion plan.
        atoms_written = 0
        try:
            if media_result.get("notes_written", 0) > 0:
                atoms_written = await _post_process_notes_for_atoms(
                    pool, patient_id, batch_start_ts=start_dt,
                )
        except Exception as e:
            log.warning("behavioral atom extraction step failed: %s", type(e).__name__)

        # Supplemental: route Observation + QuestionnaireResponse resources
        # through the screening registry ingestor so formal screens land
        # in behavioral_screenings / sdoh_screenings and open domain gaps
        # get resolved on the same pass. Best-effort.
        screening_summary: dict = {}
        try:
            if 'fhir_resources' in locals() and fhir_resources:
                screening_summary = await _ingest_screening_resources(
                    pool, fhir_resources, patient_id,
                )
        except Exception as e:
            log.warning("screening-registry ingest step failed: %s",
                        type(e).__name__)

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
            "notes_written": media_result.get("notes_written", 0),
            "refs_written": media_result.get("refs_written", 0),
            "behavioral_atoms_written": atoms_written,
            "behavioral_screenings_written":
                screening_summary.get("behavioral_screenings_written", 0),
            "sdoh_screenings_written":
                screening_summary.get("sdoh_screenings_written", 0),
            "domains_resolved":
                screening_summary.get("domains_resolved", []),
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

    Accepts both canonical field names (result_value, result_unit, effective_date)
    and legacy aliases (value, unit, date) for backward compatibility.
    """
    out = []
    for item in items:
        if not isinstance(item, dict):
            continue
        code = item.get("loinc") or item.get("loinc_code") or item.get("code") or ""
        display = item.get("name") or item.get("display") or item.get("test_name") or ""
        # Accept new canonical field names and legacy aliases
        unit = (item.get("result_unit") or item.get("unit")
                or item.get("units") or "")
        date = (item.get("effective_date") or item.get("date")
                or item.get("effectiveDateTime")
                or item.get("collected_date") or item.get("resulted_date") or "")
        raw_val = (item.get("result_value") or item.get("value")
                   or item.get("result") or item.get("numeric_value"))
        if raw_val is None:
            raw_val = ""
        ref_range = item.get("ref_range") or item.get("reference_range") or ""
        is_abnormal = item.get("flag") == "out_of_range" or item.get("status") == "out_of_range"

        # Try numeric conversion; preserve qualitative text separately
        result_text = None
        try:
            numeric = float(str(raw_val).split()[0])
        except (ValueError, TypeError, IndexError):
            numeric = 0.0
            if raw_val:
                result_text = str(raw_val)

        obs = {
            "resourceType": "Observation",
            "code": {"coding": [{"code": code, "display": display}]},
            "valueQuantity": {"value": numeric, "unit": unit},
            "effectiveDateTime": date,
            "_is_abnormal": is_abnormal,
        }
        if result_text:
            obs["_result_text"] = result_text
        if ref_range:
            obs["_reference_text"] = ref_range
        if code:
            obs["_loinc_code"] = code
        out.append(obs)
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
            raw_mt = rec.get("metric_type", "")
            metric_type = raw_mt.lower().replace(" ", "_")
            await conn.execute(
                """INSERT INTO biometric_readings
                       (id, patient_id, metric_type, value, unit,
                        measured_at, is_abnormal,
                        result_text, result_numeric, result_unit,
                        reference_text, loinc_code, data_source)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)
                   ON CONFLICT (patient_id, metric_type, measured_at)
                   DO UPDATE SET
                       value        = EXCLUDED.value,
                       unit         = EXCLUDED.unit,
                       is_abnormal  = EXCLUDED.is_abnormal,
                       result_text  = EXCLUDED.result_text,
                       result_numeric = EXCLUDED.result_numeric,
                       result_unit  = EXCLUDED.result_unit,
                       reference_text = EXCLUDED.reference_text,
                       loinc_code   = EXCLUDED.loinc_code,
                       data_source  = EXCLUDED.data_source""",
                rec.get("id", str(_uuid_mod.uuid4())),
                rec["patient_id"], metric_type,
                rec.get("value"), rec.get("unit", ""),
                rec.get("measured_at"),
                rec.get("is_abnormal", False),
                rec.get("result_text"),
                rec.get("result_numeric"),
                rec.get("result_unit") or rec.get("unit", ""),
                rec.get("reference_text"),
                rec.get("loinc_code"),
                "healthex",
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

_ROUTABLE_TYPES = {"Binary", "DocumentReference", "Observation",
                   "Practitioner", "QuestionnaireResponse"}


async def _ingest_screening_resources(
    pool, fhir_resources: list[dict], patient_id: str,
) -> dict:
    """Hook: feed Observation + QuestionnaireResponse resources through
    the registry-driven screening ingestor (mcp-server/skills/
    behavioral_screening_ingestor.py). For any row that writes to
    behavioral_screenings, resolve the open gap in that domain. Also
    dispatches SDoH instruments to sdoh_screenings.

    Returns a summary dict. Best-effort — logs and swallows all errors.
    """
    summary = {
        "behavioral_screenings_written": 0,
        "sdoh_screenings_written": 0,
        "domains_resolved": [],
    }
    if not fhir_resources:
        return summary
    try:
        from skills.behavioral_screening_ingestor import ingest_fhir_resource
        from skills.behavioral_gap_detector import (
            resolve_gap_on_new_screening,
            run_gap_detector_for_patient,
        )
    except Exception as e:
        log.warning("screening ingestor libs unavailable: %s", type(e).__name__)
        return summary

    try:
        async with pool.acquire() as conn:
            for res in fhir_resources:
                rtype = res.get("resourceType") if isinstance(res, dict) else None
                if rtype not in ("Observation", "QuestionnaireResponse"):
                    continue
                try:
                    r = await ingest_fhir_resource(conn, res, patient_id)
                except Exception as e:
                    log.warning("ingest_fhir_resource failed: %s", type(e).__name__)
                    continue
                if not r:
                    continue
                if r.get("behavioral_screening_id"):
                    summary["behavioral_screenings_written"] += 1
                    try:
                        from datetime import date as _d
                        obs_d = r.get("observation_date")
                        try:
                            obs_d = _d.fromisoformat(obs_d) if obs_d else _d.today()
                        except Exception:
                            obs_d = _d.today()
                        await resolve_gap_on_new_screening(
                            conn=conn,
                            patient_id=patient_id,
                            new_screening_id=r["behavioral_screening_id"],
                            instrument_key=r.get("instrument_key"),
                            domain=r.get("domain"),
                            screening_date=obs_d,
                        )
                        summary["domains_resolved"].append(r.get("domain"))
                    except Exception as e:
                        log.warning("resolve_gap_on_new_screening failed: %s",
                                    type(e).__name__)
                if r.get("sdoh_screening_id"):
                    summary["sdoh_screenings_written"] += 1

            # Re-run the domain-driven detector so any domain now uncovered
            # can get a fresh gap opened on the same pass.
            try:
                await run_gap_detector_for_patient(conn, patient_id)
            except Exception as e:
                log.warning("post-ingest gap detector failed: %s",
                            type(e).__name__)
    except Exception as e:
        log.warning("screening-resource ingest failed: %s", type(e).__name__)
    return summary


def _extract_routable_resources(raw: str) -> list[dict]:
    """
    Parse raw JSON text and extract FHIR resources that the content router
    can handle (Binary, DocumentReference, Observation with valueString,
    Practitioner with photo).

    Returns a list of FHIR resource dicts, or [] if parsing fails.
    """
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        # BUG 3: previously swallowed silently. Log enough context to
        # diagnose unescaped-char crashes without leaking full payload.
        preview = raw[:120] if isinstance(raw, str) else repr(raw)[:120]
        log.warning(
            "routable-resource JSON parse failed at col=%s msg=%s preview=%r",
            getattr(exc, "colno", "?"), exc.msg, preview,
        )
        return []
    except Exception as exc:
        log.warning("routable-resource parse error: %s", exc)
        return []

    resources = []

    if isinstance(parsed, dict):
        rtype = parsed.get("resourceType", "")
        if rtype in _ROUTABLE_TYPES:
            resources.append(parsed)
        elif rtype == "Bundle" and "entry" in parsed:
            for entry in parsed.get("entry", []):
                res = entry.get("resource", {})
                if res.get("resourceType") in _ROUTABLE_TYPES:
                    resources.append(res)
    elif isinstance(parsed, list):
        for item in parsed:
            if isinstance(item, dict) and item.get("resourceType") in _ROUTABLE_TYPES:
                resources.append(item)

    # Only keep Observation resources that have valueString OR a LOINC
    # code in the behavioral / SDoH registry (those carry total_score
    # via valueQuantity / valueInteger and item scores via component[]).
    # Numeric NON-behavioral Observations remain handled by the
    # structured path.
    try:
        from skills.screening_registry import LOINC_TO_INSTRUMENT
    except Exception:
        LOINC_TO_INSTRUMENT = {}
    try:
        from skills.sdoh_registry import SDOH_LOINC_TO_INSTRUMENT
    except Exception:
        SDOH_LOINC_TO_INSTRUMENT = {}

    def _is_screening_loinc(res: dict) -> bool:
        for c in (res.get("code", {}) or {}).get("coding", []) or []:
            code = c.get("code")
            if not code:
                continue
            if code in LOINC_TO_INSTRUMENT or code in SDOH_LOINC_TO_INSTRUMENT:
                return True
        return False

    filtered = []
    for r in resources:
        rtype = r.get("resourceType", "")
        if rtype == "Observation":
            if r.get("valueString") or _is_screening_loinc(r):
                filtered.append(r)
        else:
            filtered.append(r)

    return filtered
