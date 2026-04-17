"""
traced_writer.py — Per-record async writer with full transfer_log audit trail.

Replaces the bulk write approach. Every clinical record goes through:
    planned → sanitized → written → verified
with a timestamp logged at each stage in the transfer_log table.

Blob escaping (unescaped double-quotes, null bytes) is applied at the
sanitize stage — before any data touches the warehouse or FHIR transforms.

All functions are async (asyncpg connection pool).
No circular imports: all native→FHIR converters are inlined here.
"""

import logging
import sys
import uuid as _uuid_mod
from datetime import datetime, timezone
from pathlib import Path

from .transfer_planner import TransferRecord, TransferPlan, now_utc

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Blob sanitizer — fixes the unescaped double-quote bug
# ---------------------------------------------------------------------------

def sanitize_text_field(value: str, max_len: int = 10_000) -> str:
    """
    Clean any string field before it reaches the DB.
    - Protects clinical notation (blood types, comparators, gene variants, etc.)
    - Removes injection vectors (Unicode smuggling, LLM control tokens, role injection)
    - Replaces double-quotes with single-quotes (fixes the ultrasound narrative bug)
    - Strips null bytes that break JSON serialization
    - Truncates to max_len to prevent context blowout
    """
    if not isinstance(value, str):
        return value
    from ingestion.sanitization.clinical_sanitizer import clinical_sanitize
    return clinical_sanitize(value, max_len=max_len)


def sanitize_row(row: dict) -> dict:
    """Apply sanitize_text_field to every string value in a row dict."""
    return {
        k: sanitize_text_field(v) if isinstance(v, str) else v
        for k, v in row.items()
    }


# ---------------------------------------------------------------------------
# Native → FHIR → DB schema (inlined to avoid circular import with executor)
# ---------------------------------------------------------------------------

def _native_to_fhir_one(resource_type: str, item: dict) -> dict | None:
    """Convert a single native HealthEx item to a minimal FHIR resource."""
    if resource_type == "labs":
        code = item.get("loinc") or item.get("loinc_code") or item.get("code") or ""
        display = (item.get("name") or item.get("test_name")
                   or item.get("display") or "")
        # Accept both new canonical field names and legacy aliases
        unit = (item.get("result_unit") or item.get("unit")
                or item.get("units") or "")
        date = (item.get("effective_date") or item.get("date")
                or item.get("effectiveDateTime")
                or item.get("collected_date") or item.get("resulted_date") or "")
        raw_val = (item.get("result_value") or item.get("value")
                   or item.get("result") or item.get("numeric_value") or "")
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
        # Pass-through fields for structured storage
        if result_text:
            obs["_result_text"] = result_text
        if ref_range:
            obs["_reference_text"] = ref_range
        if code:
            obs["_loinc_code"] = code
        return obs

    elif resource_type == "conditions":
        code = item.get("icd10") or item.get("icd10_code") or item.get("code") or ""
        display = (item.get("name") or item.get("display")
                   or item.get("description") or "")
        status = item.get("status") or "active"
        onset = (item.get("onset_date") or item.get("onsetDate")
                 or item.get("diagnosed_date") or item.get("onset") or "")
        return {
            "resourceType": "Condition",
            "code": {"coding": [{"code": code, "display": display,
                                  "system": "http://hl7.org/fhir/sid/icd-10"}]},
            "clinicalStatus": {"coding": [{"code": status}]},
            "onsetDateTime": onset,
        }

    elif resource_type == "medications":
        code = item.get("rxnorm") or item.get("code") or ""
        display = (item.get("name") or item.get("display")
                   or item.get("drug_name") or "")
        status = item.get("status") or "active"
        authored = (item.get("start_date") or item.get("authoredOn")
                    or item.get("prescribed_date") or "")
        return {
            "resourceType": "MedicationRequest",
            "medicationCodeableConcept": {"coding": [{"code": code,
                                                       "display": display}]},
            "status": status,
            "authoredOn": authored,
        }

    elif resource_type == "encounters":
        enc_type = (item.get("type") or item.get("encounter_type")
                    or item.get("visit_type") or "encounter")
        date = (item.get("date") or item.get("start_date")
                or item.get("encounter_date") or item.get("visit_date") or "")
        return {
            "resourceType": "Encounter",
            "type": [{"coding": [{"display": enc_type}]}],
            "period": {"start": date},
        }

    return None


def _fhir_to_db_one(resource_type: str, fhir: dict,
                    patient_id: str) -> dict | None:
    """Convert a single FHIR resource to a DB row dict."""
    from datetime import datetime as _dt

    if resource_type == "labs":
        coding = fhir.get("code", {}).get("coding", [{}])[0]
        vq = fhir.get("valueQuantity", {})
        dt_str = fhir.get("effectiveDateTime", "")
        try:
            measured_at = _dt.fromisoformat(str(dt_str)) if dt_str else _dt.utcnow()
        except (ValueError, TypeError):
            measured_at = _dt.utcnow()
        raw_display = coding.get("display", "") or coding.get("code", "")
        # Normalize: lowercase + spaces→underscores so metric_type is consistent
        metric_type = raw_display.lower().replace(" ", "_")
        is_abnormal = bool(fhir.get("_is_abnormal", False))
        unit = vq.get("unit", "")
        numeric_val = vq.get("value")
        return {
            "id": str(_uuid_mod.uuid4()),
            "patient_id": patient_id,
            "metric_type": metric_type,
            "value": numeric_val,
            "unit": unit,
            "measured_at": measured_at,
            "is_abnormal": is_abnormal,
            # New structured fields
            "result_text": fhir.get("_result_text"),
            "result_numeric": numeric_val if numeric_val != 0.0 or not fhir.get("_result_text") else None,
            "result_unit": unit,
            "reference_text": fhir.get("_reference_text"),
            "loinc_code": fhir.get("_loinc_code"),
            "data_source": "healthex",
        }

    elif resource_type == "conditions":
        coding = fhir.get("code", {}).get("coding", [{}])[0]
        status_coding = fhir.get("clinicalStatus", {}).get("coding", [{}])[0]
        onset = fhir.get("onsetDateTime", "")
        try:
            from datetime import date as _date
            onset_date = _date.fromisoformat(str(onset)[:10]) if onset else None
        except (ValueError, TypeError):
            onset_date = None
        return {
            "id": str(_uuid_mod.uuid4()),
            "patient_id": patient_id,
            "code": coding.get("code", ""),
            "display": coding.get("display", ""),
            "onset_date": onset_date,
            "clinical_status": status_coding.get("code", "active"),
            "data_source": "healthex",
        }

    elif resource_type == "medications":
        coding = (fhir.get("medicationCodeableConcept", {})
                      .get("coding", [{}])[0])
        authored = fhir.get("authoredOn", "")
        try:
            from datetime import date as _date
            authored_on = _date.fromisoformat(str(authored)[:10]) if authored else None
        except (ValueError, TypeError):
            authored_on = None
        return {
            "id": str(_uuid_mod.uuid4()),
            "patient_id": patient_id,
            "code": coding.get("code", ""),
            "display": coding.get("display", ""),
            "status": fhir.get("status", "active"),
            "authored_on": authored_on,
            "data_source": "healthex",
        }

    elif resource_type == "encounters":
        type_coding = (fhir.get("type", [{}])[0]
                       if fhir.get("type") else {})
        type_display = (type_coding.get("coding", [{}])[0].get("display", "encounter")
                        if isinstance(type_coding, dict) else "encounter")
        period_start = fhir.get("period", {}).get("start", "")
        from datetime import datetime as _dt
        try:
            event_date = _dt.fromisoformat(str(period_start)) if period_start else _dt.utcnow()
        except (ValueError, TypeError):
            event_date = _dt.utcnow()
        return {
            "id": str(_uuid_mod.uuid4()),
            "patient_id": patient_id,
            "event_type": type_display,
            "event_date": event_date,
            "description": "",
            "data_source": "healthex",
        }

    return None


# ---------------------------------------------------------------------------
# transfer_log async helpers
# ---------------------------------------------------------------------------

async def _log_transfer_async(conn, tr: TransferRecord, patient_id: str,
                               payload_bytes: int) -> None:
    """Insert the initial PLANNED row into transfer_log."""
    try:
        await conn.execute(
            """INSERT INTO transfer_log
                   (id, patient_id, resource_type, source,
                    record_key, record_hash, loinc_code, icd10_code, encounter_id,
                    batch_id, batch_sequence, batch_total,
                    chunk_id, chunk_sequence, chunk_total,
                    strategy, format_detected, planned_at, payload_size_bytes, status)
               VALUES
                   ($1::uuid, $2::uuid, $3, $4,
                    $5, $6, $7, $8, $9,
                    $10::uuid, $11, $12,
                    $13::uuid, $14, $15,
                    $16, $17, $18, $19, 'planned')
               ON CONFLICT (id) DO NOTHING""",
            tr.transfer_id, patient_id, tr.resource_type, tr.source,
            tr.record_key, tr.record_hash,
            tr.loinc_code or None, tr.icd10_code or None, tr.encounter_id or None,
            tr.batch_id, tr.batch_sequence, tr.batch_total,
            tr.chunk_id, tr.chunk_sequence, tr.chunk_total,
            tr.strategy, tr.format_detected or None, tr.planned_at, payload_bytes,
        )
    except Exception as e:
        log.warning("transfer_log insert failed for %s: %s", tr.record_key, e)


async def _update_transfer_async(conn, transfer_id: str, updates: dict) -> None:
    """Update a transfer_log row by its id (primary key = transfer_id)."""
    if not updates:
        return
    set_parts = []
    values = []
    for i, (col, val) in enumerate(updates.items(), start=1):
        set_parts.append(f"{col} = ${i}")
        values.append(val)
    values.append(transfer_id)
    sql = (
        f"UPDATE transfer_log SET {', '.join(set_parts)} "
        f"WHERE id = ${len(values)}::uuid"
    )
    try:
        await conn.execute(sql, *values)
    except Exception as e:
        log.warning("transfer_log update failed for %s: %s", transfer_id, e)


async def log_single_record_transfer(
    conn,
    patient_id: str,
    resource_type: str,
    source: str,
    record_key: str,
    strategy: str,
    format_detected: str,
    payload_bytes: int,
    *,
    loinc_code: str = "",
    icd10_code: str = "",
    encounter_id: str = "",
    record_hash: str = "",
    mark_verified: bool = False,
) -> str:
    """Emit a single transfer_log row for paths that bypass execute_transfer_plan.

    Returns the transfer_id (UUID string) for follow-up updates.

    PHI rule: record_key must be a natural key (coded identifiers, dates,
    instrument names) — never patient name, DOB, or free-text clinical content.
    """
    import hashlib as _hashlib
    batch_id = str(_uuid_mod.uuid4())
    now = now_utc()
    tr = TransferRecord(
        batch_id=batch_id,
        batch_sequence=1,
        batch_total=1,
        chunk_id=batch_id,
        chunk_sequence=1,
        chunk_total=1,
        row={},
        record_key=record_key,
        record_hash=record_hash or _hashlib.sha256(record_key.encode()).hexdigest()[:16],
        loinc_code=loinc_code,
        icd10_code=icd10_code,
        encounter_id=encounter_id,
        resource_type=resource_type,
        source=source,
        format_detected=format_detected,
        strategy=strategy,
        planned_at=now,
    )
    await _log_transfer_async(conn, tr, patient_id, payload_bytes)
    if mark_verified:
        await _update_transfer_async(conn, tr.transfer_id, {
            "status": "verified",
            "sanitized_at": now,
            "written_at": now,
            "verified_at": now,
        })
    return tr.transfer_id


# ---------------------------------------------------------------------------
# Per-record DB writers
# ---------------------------------------------------------------------------

async def _write_one_record(conn, resource_type: str, db_rec: dict) -> bool:
    """Write a single DB record. Returns True on success."""
    from datetime import datetime as _dt

    try:
        if resource_type == "labs":
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
                db_rec["id"], db_rec["patient_id"],
                db_rec.get("metric_type", ""),
                db_rec.get("value"),
                db_rec.get("unit", ""),
                db_rec.get("measured_at") or _dt.utcnow(),
                db_rec.get("is_abnormal", False),
                db_rec.get("result_text"),
                db_rec.get("result_numeric"),
                db_rec.get("result_unit") or db_rec.get("unit", ""),
                db_rec.get("reference_text"),
                db_rec.get("loinc_code"),
                "healthex",
            )
        elif resource_type == "conditions":
            await conn.execute(
                """INSERT INTO patient_conditions
                       (id, patient_id, code, display, onset_date,
                        clinical_status, data_source)
                   VALUES ($1,$2,$3,$4,$5,$6,$7)
                   ON CONFLICT (natural_key) DO UPDATE SET
                       clinical_status = EXCLUDED.clinical_status,
                       data_source     = EXCLUDED.data_source""",
                db_rec["id"], db_rec["patient_id"],
                db_rec.get("code", ""),
                db_rec.get("display", ""),
                db_rec.get("onset_date"),
                db_rec.get("clinical_status", "active"),
                "healthex",
            )
        elif resource_type == "medications":
            await conn.execute(
                """INSERT INTO patient_medications
                       (id, patient_id, code, display, status,
                        authored_on, data_source)
                   VALUES ($1,$2,$3,$4,$5,$6,$7)
                   ON CONFLICT (natural_key) DO UPDATE SET
                       status      = EXCLUDED.status,
                       data_source = EXCLUDED.data_source""",
                db_rec["id"], db_rec["patient_id"],
                db_rec.get("code", ""),
                db_rec.get("display", ""),
                db_rec.get("status", "active"),
                db_rec.get("authored_on"),
                "healthex",
            )
        elif resource_type == "encounters":
            await conn.execute(
                """INSERT INTO clinical_events
                       (id, patient_id, event_type, event_date,
                        description, data_source)
                   VALUES ($1,$2,$3,$4,$5,$6)
                   ON CONFLICT (natural_key) DO UPDATE SET
                       data_source = EXCLUDED.data_source""",
                db_rec["id"], db_rec["patient_id"],
                db_rec.get("event_type", "encounter"),
                db_rec.get("event_date") or _dt.utcnow(),
                db_rec.get("description", ""),
                "healthex",
            )
        else:
            log.warning("traced_writer: unknown resource_type %s", resource_type)
            return False
        return True
    except Exception as e:
        log.warning("_write_one_record failed for %s: %s", resource_type, e)
        return False


async def _verify_one_record(conn, resource_type: str, db_rec: dict) -> bool:
    """Verify a single record was actually written."""
    pid = db_rec.get("patient_id")
    try:
        if resource_type == "labs":
            cnt = await conn.fetchval(
                """SELECT COUNT(*) FROM biometric_readings
                   WHERE patient_id = $1::uuid AND metric_type = $2""",
                pid, db_rec.get("metric_type", ""),
            )
        elif resource_type == "conditions":
            cnt = await conn.fetchval(
                """SELECT COUNT(*) FROM patient_conditions
                   WHERE patient_id = $1::uuid AND code = $2""",
                pid, db_rec.get("code", ""),
            )
        elif resource_type == "medications":
            cnt = await conn.fetchval(
                """SELECT COUNT(*) FROM patient_medications
                   WHERE patient_id = $1::uuid AND code = $2""",
                pid, db_rec.get("code", ""),
            )
        elif resource_type == "encounters":
            cnt = await conn.fetchval(
                """SELECT COUNT(*) FROM clinical_events
                   WHERE patient_id = $1::uuid AND event_type = $2""",
                pid, db_rec.get("event_type", "encounter"),
            )
        else:
            return False
        return (cnt or 0) > 0
    except Exception as e:
        log.warning("_verify_one_record query error: %s", e)
        return False


# ---------------------------------------------------------------------------
# Main traced executor (async, asyncpg)
# ---------------------------------------------------------------------------

async def execute_transfer_plan_async(pool, plan: TransferPlan,
                                      patient_id: str) -> dict:
    """
    Execute a TransferPlan record-by-record with full transfer_log audit trail.

    For each record:
      1. Log PLANNED in transfer_log
      2. Sanitize (blob escape, truncate)
      3. Native → FHIR normalize (inlined — no circular import)
      4. FHIR → DB schema transform (inlined)
      5. Write to warehouse table (INSERT … ON CONFLICT DO NOTHING)
      6. Verify with SELECT COUNT
      7. Log VERIFIED or FAILED

    Returns summary dict compatible with ingest_from_healthex response schema.
    """
    records_written = 0
    records_verified = 0
    records_failed = 0

    log.info(
        "TracedWriter: patient=%s resource=%s strategy=%s total=%d chunks=%d batch=%s",
        patient_id, plan.resource_type, plan.strategy,
        plan.total_records, len(plan.chunks), plan.batch_id,
    )

    async with pool.acquire() as conn:
        for chunk_idx, chunk in enumerate(plan.chunks):
            log.info("Chunk %d/%d: %d records",
                     chunk_idx + 1, len(plan.chunks), len(chunk))

            for tr in chunk:
                # ── 1. Log PLANNED ──────────────────────────────────────────
                await _log_transfer_async(conn, tr, patient_id, plan.payload_bytes)

                # ── 2. Sanitize ─────────────────────────────────────────────
                try:
                    sanitized = sanitize_row(tr.row)
                    sanitized["patient_id"] = patient_id
                    tr.sanitized_at = now_utc()
                    await _update_transfer_async(conn, tr.transfer_id, {
                        "status": "sanitized",
                        "sanitized_at": tr.sanitized_at,
                    })
                except Exception as e:
                    records_failed += 1
                    await _update_transfer_async(conn, tr.transfer_id, {
                        "status": "failed",
                        "failed_at": now_utc(),
                        "error_stage": "sanitize",
                        "error_message": str(e)[:500],
                    })
                    log.error("Sanitize failed for %s: %s", tr.record_key, e)
                    continue

                # ── 3+4. Native → FHIR → DB schema ─────────────────────────
                try:
                    fhir_res = _native_to_fhir_one(plan.resource_type, sanitized)
                    if fhir_res is None:
                        raise ValueError("normalize returned None")

                    # F1c: FHIR structural validation (annotates, never discards)
                    try:
                        from ingestion.validators.fhir_validator import validate_fhir_resource
                        is_valid, fhir_issues = validate_fhir_resource(sanitized, plan.resource_type)
                        if not is_valid:
                            log.warning(
                                "FHIR validation issues for %s: %s",
                                tr.record_key, fhir_issues[:3],
                            )
                    except Exception as _fhir_exc:
                        log.debug("FHIR validator skipped: %s", _fhir_exc)

                    db_rec = _fhir_to_db_one(plan.resource_type, fhir_res, patient_id)
                    if db_rec is None:
                        raise ValueError("transform returned None")

                    # F3: Clinical plausibility validation for labs (annotates)
                    if plan.resource_type == "labs":
                        try:
                            from ingestion.validators.plausibility import validate_plausibility
                            validate_plausibility(
                                db_rec, resource_type=plan.resource_type,
                                patient_mrn=str(patient_id),
                            )
                            if db_rec.get("quality_status") == "flagged":
                                log.warning(
                                    "Plausibility flagged record %s: %s",
                                    tr.record_key,
                                    db_rec.get("quality_flags", [{}])[0].get("note", ""),
                                )
                        except Exception as _plaus_exc:
                            log.debug("Plausibility validator skipped: %s", _plaus_exc)

                    tr.extracted_at = now_utc()
                except Exception as e:
                    records_failed += 1
                    await _update_transfer_async(conn, tr.transfer_id, {
                        "status": "failed",
                        "failed_at": now_utc(),
                        "error_stage": "transform",
                        "error_message": str(e)[:500],
                    })
                    log.error("Transform failed for %s: %s", tr.record_key, e)
                    continue

                # ── 5. Write ─────────────────────────────────────────────────
                write_ok = await _write_one_record(conn, plan.resource_type, db_rec)
                if not write_ok:
                    records_failed += 1
                    await _update_transfer_async(conn, tr.transfer_id, {
                        "status": "failed",
                        "failed_at": now_utc(),
                        "error_stage": "write",
                        "error_message": "INSERT returned False",
                    })
                    continue

                records_written += 1
                await _update_transfer_async(conn, tr.transfer_id, {
                    "status": "written",
                    "written_at": now_utc(),
                })

                # ── 6. Verify ────────────────────────────────────────────────
                verified = await _verify_one_record(conn, plan.resource_type, db_rec)
                if verified:
                    records_verified += 1
                    await _update_transfer_async(conn, tr.transfer_id, {
                        "status": "verified",
                        "verified_at": now_utc(),
                    })
                    log.debug("Verified: %s", tr.record_key)
                else:
                    await _update_transfer_async(conn, tr.transfer_id, {
                        "status": "written_unverified",
                        "error_stage": "verify",
                        "error_message": "Row not found after write",
                    })
                    log.warning("Written but unverified: %s", tr.record_key)

    total = plan.total_records
    status = (
        "ok"      if records_failed == 0 and records_written > 0
        else "partial" if records_written > 0
        else "failed"  if total > 0
        else "ok"
    )

    log.info(
        "TracedWriter complete: batch=%s written=%d verified=%d failed=%d status=%s",
        plan.batch_id, records_written, records_verified, records_failed, status,
    )

    return {
        "status":           status,
        "patient_id":       patient_id,
        "resource_type":    plan.resource_type,
        "batch_id":         plan.batch_id,
        "strategy":         plan.strategy,
        "records_planned":  total,
        "records_written":  records_written,
        "records_verified": records_verified,
        "records_failed":   records_failed,
        "chunks_total":     len(plan.chunks),
        "format_detected":  plan.format_detected,
    }
