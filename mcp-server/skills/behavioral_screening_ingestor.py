"""
behavioral_screening_ingestor.py — Parse FHIR Observation / QuestionnaireResponse
resources and write rows to behavioral_screenings + (optionally) behavioral_signal_atoms.

Designed to be called from:
  - ingestion/adapters/healthex/executor.py  (_ingest_observation_or_qr)
  - MCP tool ingest_behavioral_screening_fhir (defined in behavioral_atoms.py)

All functions are async (asyncpg).
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger(__name__)


def _parse_datetime(value: str | None) -> Optional[datetime]:
    if not value:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


async def ingest_fhir_observation(
    pool,
    patient_id: str,
    resource: dict,
    source_type: str = "fhir_observation",
    source_id: Optional[str] = None,
    data_source: str = "healthex",
) -> Optional[dict]:
    """Parse a FHIR Observation that is a behavioral screening questionnaire panel.

    Returns inserted row summary or None if the LOINC code is not in the registry.
    """
    from skills.screening_registry import (
        get_instrument_for_loinc,
        get_severity_band,
        get_triggered_critical_items,
    )

    if resource.get("resourceType") != "Observation":
        return None

    # Extract LOINC from code.coding
    loinc_code: Optional[str] = None
    for coding in resource.get("code", {}).get("coding", []):
        sys = coding.get("system", "")
        if "loinc" in sys.lower() or coding.get("code", "").replace("-", "").isdigit():
            loinc_code = coding.get("code")
            break

    if not loinc_code:
        return None

    instrument = get_instrument_for_loinc(loinc_code)
    if not instrument:
        return None

    # Extract score from valueQuantity or valueInteger or component sum
    score: Optional[int] = None
    vq = resource.get("valueQuantity")
    if vq and vq.get("value") is not None:
        try:
            score = int(float(vq["value"]))
        except (TypeError, ValueError):
            pass

    vi = resource.get("valueInteger")
    if score is None and vi is not None:
        try:
            score = int(vi)
        except (TypeError, ValueError):
            pass

    # Extract item-level answers from component array
    item_answers: dict[int, int] = {}
    for comp in resource.get("component", []):
        item_num = None
        # item number often encoded in component code
        for coding in comp.get("code", {}).get("coding", []):
            text = coding.get("display", "") or coding.get("code", "")
            # Try to extract trailing number, e.g. "Q9" or "Item 9"
            import re
            m = re.search(r"\d+", text)
            if m:
                item_num = int(m.group())
                break
        if item_num is None:
            continue
        vq_c = comp.get("valueQuantity", {})
        vi_c = comp.get("valueInteger")
        val: Optional[int] = None
        if vq_c.get("value") is not None:
            try:
                val = int(float(vq_c["value"]))
            except (TypeError, ValueError):
                pass
        if val is None and vi_c is not None:
            try:
                val = int(vi_c)
            except (TypeError, ValueError):
                pass
        if val is not None:
            item_answers[item_num] = val

    # Compute score from items if not directly in resource
    if score is None and item_answers:
        score = sum(item_answers.values())

    band_label: Optional[str] = None
    if score is not None:
        band = get_severity_band(instrument.key, score)
        band_label = band.label if band else None

    triggered_critical = []
    if item_answers:
        for ci in get_triggered_critical_items(instrument.key, item_answers):
            triggered_critical.append({
                "item_number": ci.item_number,
                "alert_text": ci.alert_text,
                "actual_score": item_answers.get(ci.item_number, 0),
                "threshold": ci.threshold,
            })

    administered_at = _parse_datetime(
        resource.get("effectiveDateTime") or resource.get("issued")
    ) or datetime.now(timezone.utc)

    row_id = str(uuid.uuid4())

    import json as _json
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO behavioral_screenings
                (id, patient_id, instrument_key, domain, loinc_code, score,
                 band, item_answers, triggered_critical, source_type, source_id,
                 administered_at, data_source)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8::jsonb,$9::jsonb,$10,$11,$12,$13)
            ON CONFLICT DO NOTHING
            """,
            row_id, patient_id, instrument.key, instrument.domain,
            loinc_code, score, band_label,
            _json.dumps(item_answers),
            _json.dumps(triggered_critical),
            source_type, source_id, administered_at, data_source,
        )

    log.info(
        "ingest_fhir_observation: patient=%s instrument=%s score=%s band=%s critical=%d",
        patient_id, instrument.key, score, band_label, len(triggered_critical),
    )

    return {
        "id": row_id,
        "instrument_key": instrument.key,
        "domain": instrument.domain,
        "score": score,
        "band": band_label,
        "critical_count": len(triggered_critical),
        "triggered_critical": triggered_critical,
    }


async def ingest_fhir_questionnaire_response(
    pool,
    patient_id: str,
    resource: dict,
    source_type: str = "questionnaire_response",
    source_id: Optional[str] = None,
    data_source: str = "healthex",
) -> Optional[dict]:
    """Parse a FHIR QuestionnaireResponse (QR) for a known behavioral screener.

    Looks up the questionnaire URL/canonical to find the LOINC code, then
    delegates to item-level parsing.
    """
    from skills.screening_registry import (
        get_instrument_for_loinc,
        get_severity_band,
        get_triggered_critical_items,
    )

    if resource.get("resourceType") != "QuestionnaireResponse":
        return None

    # Try to resolve LOINC from questionnaire reference
    questionnaire_ref = resource.get("questionnaire", "")
    loinc_code: Optional[str] = None

    # Many EHRs embed the LOINC in the questionnaire URL, e.g. "Questionnaire/44249-1"
    import re
    m = re.search(r"(\d{5}-\d)", questionnaire_ref)
    if m:
        loinc_code = m.group(1)

    if not loinc_code:
        return None

    instrument = get_instrument_for_loinc(loinc_code)
    if not instrument:
        return None

    # Parse item-level answers (QR uses nested items)
    item_answers: dict[int, int] = {}
    for item in resource.get("item", []):
        link_id = item.get("linkId", "")
        m2 = re.search(r"\d+", link_id)
        item_num = int(m2.group()) if m2 else None
        if item_num is None:
            continue
        for ans in item.get("answer", []):
            val_int = ans.get("valueInteger")
            val_dec = ans.get("valueDecimal")
            val = val_int if val_int is not None else val_dec
            if val is not None:
                try:
                    item_answers[item_num] = int(float(val))
                except (TypeError, ValueError):
                    pass
                break

    score: Optional[int] = sum(item_answers.values()) if item_answers else None

    band_label: Optional[str] = None
    if score is not None:
        band = get_severity_band(instrument.key, score)
        band_label = band.label if band else None

    triggered_critical = []
    if item_answers:
        for ci in get_triggered_critical_items(instrument.key, item_answers):
            triggered_critical.append({
                "item_number": ci.item_number,
                "alert_text": ci.alert_text,
                "actual_score": item_answers.get(ci.item_number, 0),
                "threshold": ci.threshold,
            })

    authored_at = _parse_datetime(resource.get("authored")) or datetime.now(timezone.utc)

    row_id = str(uuid.uuid4())

    import json as _json
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO behavioral_screenings
                (id, patient_id, instrument_key, domain, loinc_code, score,
                 band, item_answers, triggered_critical, source_type, source_id,
                 administered_at, data_source)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8::jsonb,$9::jsonb,$10,$11,$12,$13)
            ON CONFLICT DO NOTHING
            """,
            row_id, patient_id, instrument.key, instrument.domain,
            loinc_code, score, band_label,
            _json.dumps(item_answers),
            _json.dumps(triggered_critical),
            source_type, source_id, authored_at, data_source,
        )

    return {
        "id": row_id,
        "instrument_key": instrument.key,
        "domain": instrument.domain,
        "score": score,
        "band": band_label,
        "critical_count": len(triggered_critical),
        "triggered_critical": triggered_critical,
    }


async def ingest_observation_or_qr(
    pool,
    patient_id: str,
    resource: dict,
    source_type: str = "fhir_observation",
    source_id: Optional[str] = None,
    data_source: str = "healthex",
) -> Optional[dict]:
    """Route FHIR Observation or QuestionnaireResponse to the right ingestor."""
    rt = resource.get("resourceType", "")
    if rt == "Observation":
        return await ingest_fhir_observation(
            pool, patient_id, resource, source_type, source_id, data_source
        )
    elif rt == "QuestionnaireResponse":
        return await ingest_fhir_questionnaire_response(
            pool, patient_id, resource, source_type, source_id, data_source
        )
    return None


class _ConnPool:
    """Minimal asyncpg-pool-like adapter wrapping a single connection.

    Lets functions that call `pool.acquire()` work with a bare connection.
    """
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        return self

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *_args):
        pass


async def ingest_fhir_resource(
    conn,
    resource: dict,
    patient_id: str,
    source_type: str = "fhir_observation",
    source_id: Optional[str] = None,
    data_source: str = "healthex",
) -> Optional[dict]:
    """Route a FHIR Observation or QuestionnaireResponse to the right ingestor.

    Accepts a raw asyncpg connection (executor has already acquired one).
    """
    pool = _ConnPool(conn)
    rt = resource.get("resourceType", "") if isinstance(resource, dict) else ""
    if rt == "Observation":
        return await ingest_fhir_observation(
            pool, patient_id, resource, source_type, source_id, data_source
        )
    elif rt == "QuestionnaireResponse":
        return await ingest_fhir_questionnaire_response(
            pool, patient_id, resource, source_type, source_id, data_source
        )
    return None
