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
        "behavioral_screening_id": row_id,
        "instrument_key": instrument.key,
        "domain": instrument.domain,
        "score": score,
        "band": band_label,
        "critical_count": len(triggered_critical),
        "triggered_critical": triggered_critical,
        "observation_date": (
            administered_at.date().isoformat()
            if hasattr(administered_at, "date") else None
        ),
    }


async def _ingest_sdoh_qr(
    pool,
    patient_id: str,
    resource: dict,
    loinc_code: str,
    sdoh_screener,
    source_type: str,
    source_id: Optional[str],
    data_source: str,
) -> dict:
    """Write a SDoH QuestionnaireResponse to sdoh_screenings.

    Returns a summary dict with sdoh_screening_id.
    """
    import json as _json
    import re

    item_answers: dict[str, str] = {}
    domains_flagged: list[str] = []
    for item in resource.get("item", []):
        link_id = item.get("linkId", "")
        for ans in item.get("answer", []):
            for val_key in ("valueString", "valueCoding", "valueBoolean", "valueInteger"):
                v = ans.get(val_key)
                if v is not None:
                    item_answers[link_id] = str(v)
                    break

    for screener_item in (sdoh_screener.items or []):
        ans_val = item_answers.get(screener_item.loinc_code, "")
        flagged_text = (screener_item.positive_if or "").lower()
        if flagged_text and ans_val and (
            flagged_text.startswith("answer =") and ans_val.lower() in flagged_text
        ):
            d = screener_item.sdoh_domain
            if d and d not in domains_flagged:
                domains_flagged.append(d)

    authored_at = _parse_datetime(resource.get("authored")) or datetime.now(timezone.utc)
    row_id = str(uuid.uuid4())

    async with pool.acquire() as conn:
        try:
            await conn.execute(
                """
                INSERT INTO sdoh_screenings
                    (id, patient_id, screener_key, panel_loinc, domains_flagged,
                     item_answers, administered_at, source_type, source_id, data_source)
                VALUES ($1::uuid, $2::uuid, $3, $4, $5::text[], $6::jsonb,
                        $7, $8, $9::uuid, $10)
                ON CONFLICT DO NOTHING
                """,
                row_id, patient_id, sdoh_screener.key, loinc_code,
                domains_flagged,
                _json.dumps(item_answers),
                authored_at, source_type, source_id, data_source,
            )
        except Exception:
            pass

    log.info(
        "_ingest_sdoh_qr: patient=%s screener=%s domains_flagged=%s",
        patient_id, sdoh_screener.key, domains_flagged,
    )

    return {
        "id": row_id,
        "sdoh_screening_id": row_id,
        "screener_key": sdoh_screener.key,
        "panel_loinc": loinc_code,
        "domains_flagged": domains_flagged,
        "observation_date": authored_at.date().isoformat(),
    }


def _resolve_qr_instrument(resource: dict):
    """Resolve the ScreeningInstrument for a QuestionnaireResponse.

    Tries four strategies in order, returning (instrument, loinc_code) or (None, None):

    1. Embedded LOINC in questionnaire field  — e.g. "Questionnaire/44249-1"
    2. Keyword match in questionnaire field   — e.g. "Questionnaire/phq9-survey"
    3. meta.tag coding with LOINC system
    4. Extension coding with LOINC system
    """
    import re
    from skills.screening_registry import (
        get_instrument_for_loinc,
        get_instrument_by_keyword,
        INSTRUMENT_KEYWORD_MAP,
    )

    questionnaire_ref = resource.get("questionnaire", "")

    # Strategy 1: LOINC numeric pattern embedded in the questionnaire URL/ref
    m = re.search(r"(\d{4,5}-\d)", questionnaire_ref)
    if m:
        loinc_code = m.group(1)
        instrument = get_instrument_for_loinc(loinc_code)
        if instrument:
            return instrument, loinc_code

    # Strategy 2: Keyword match in questionnaire URL/title/name
    instrument = get_instrument_by_keyword(questionnaire_ref)
    if instrument:
        return instrument, instrument.loinc_code

    # Also check the resource title/name extension fields
    title = resource.get("title", "") or resource.get("name", "")
    if title:
        instrument = get_instrument_by_keyword(title)
        if instrument:
            return instrument, instrument.loinc_code

    # Strategy 3: meta.tag codings (some EHRs stamp LOINC here)
    for tag in (resource.get("meta") or {}).get("tag", []):
        system = tag.get("system", "")
        code = tag.get("code", "")
        if "loinc" in system.lower() and code:
            instrument = get_instrument_for_loinc(code)
            if instrument:
                return instrument, code
        # Also try keyword on the tag display
        display = tag.get("display", "")
        if display:
            instrument = get_instrument_by_keyword(display)
            if instrument:
                return instrument, instrument.loinc_code

    # Strategy 4: Resource extensions with LOINC coding
    for ext in resource.get("extension", []):
        for vc in ext.get("valueCoding", {}) if isinstance(ext.get("valueCoding"), list) else [ext.get("valueCoding", {})]:
            if not isinstance(vc, dict):
                continue
            system = vc.get("system", "")
            code = vc.get("code", "")
            if "loinc" in system.lower() and code:
                instrument = get_instrument_for_loinc(code)
                if instrument:
                    return instrument, code

    return None, None


async def ingest_fhir_questionnaire_response(
    pool,
    patient_id: str,
    resource: dict,
    source_type: str = "questionnaire_response",
    source_id: Optional[str] = None,
    data_source: str = "healthex",
) -> Optional[dict]:
    """Parse a FHIR QuestionnaireResponse (QR) for a known behavioral screener.

    Uses _resolve_qr_instrument to find the instrument via 4 fallback strategies:
    (1) LOINC in questionnaire URL, (2) keyword in questionnaire URL/title,
    (3) meta.tag codings, (4) extension codings.

    Item numbering: prefers 1-based sequential position from the QR item list when
    linkId is a LOINC item code (5+ digits) or a non-numeric string, to prevent
    inflated item numbers that break severity banding.
    """
    import re
    import json as _json
    from skills.screening_registry import (
        get_severity_band,
        get_triggered_critical_items,
    )

    if resource.get("resourceType") != "QuestionnaireResponse":
        return None

    instrument, loinc_code = _resolve_qr_instrument(resource)

    if not instrument:
        # Try SDoH path as fallback if no behavioral instrument found
        # (use the LOINC from questionnaire ref if we can extract it)
        m_loinc = re.search(r"(\d{4,5}-\d)", resource.get("questionnaire", ""))
        raw_loinc = m_loinc.group(1) if m_loinc else None
        if raw_loinc:
            try:
                from skills.sdoh_registry import get_screener_for_panel_loinc
                sdoh_screener = get_screener_for_panel_loinc(raw_loinc)
            except Exception:
                sdoh_screener = None
            if sdoh_screener:
                return await _ingest_sdoh_qr(
                    pool, patient_id, resource, raw_loinc, sdoh_screener,
                    source_type, source_id, data_source,
                )
        return None

    # Parse item-level answers from the QR item list.
    # Item numbering strategy:
    #   - If linkId is purely numeric (e.g. "1", "9") → use it directly as item_num
    #   - If linkId looks like a LOINC item code (e.g. "44250-7") or contains
    #     non-digit chars (e.g. "phq9.q1") → use 1-based sequential position
    #     from the items list so scores add up correctly.
    item_answers: dict[int, int] = {}
    qr_items = resource.get("item", [])
    for seq_idx, item in enumerate(qr_items, start=1):
        link_id = item.get("linkId", "")
        if re.fullmatch(r"\d+", link_id):
            # Pure integer linkId — use directly
            item_num: Optional[int] = int(link_id)
        elif re.search(r"(\d{4,5}-\d)", link_id):
            # Looks like a LOINC item code — use sequential position
            item_num = seq_idx
        else:
            # Mixed alphanumeric (e.g. "phq9.q1") — extract trailing number
            m_trailing = re.search(r"(\d+)$", link_id)
            item_num = int(m_trailing.group(1)) if m_trailing else seq_idx

        for ans in item.get("answer", []):
            val_int = ans.get("valueInteger")
            val_dec = ans.get("valueDecimal")
            # valueCoding.code is used by some EHRs for ordinal responses
            val_coding_code = None
            vc = ans.get("valueCoding")
            if isinstance(vc, dict):
                try:
                    val_coding_code = int(vc.get("code", ""))
                except (TypeError, ValueError):
                    val_coding_code = None
            val = val_int if val_int is not None else (val_dec if val_dec is not None else val_coding_code)
            if val is not None:
                try:
                    item_answers[item_num] = int(float(val))
                except (TypeError, ValueError):
                    pass
                break

    score: Optional[int] = sum(item_answers.values()) if item_answers else None

    # Also check for a pre-computed total score in extension or meta
    if score is None:
        for ext in resource.get("extension", []):
            url = ext.get("url", "")
            if "score" in url.lower() or "total" in url.lower():
                v = ext.get("valueDecimal") or ext.get("valueInteger")
                if v is not None:
                    try:
                        score = int(float(v))
                    except (TypeError, ValueError):
                        pass
                    break

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

    log.info(
        "ingest_fhir_questionnaire_response: patient=%s instrument=%s score=%s band=%s critical=%d loinc=%s",
        patient_id, instrument.key, score, band_label, len(triggered_critical), loinc_code,
    )

    return {
        "id": row_id,
        "behavioral_screening_id": row_id,
        "instrument_key": instrument.key,
        "domain": instrument.domain,
        "score": score,
        "band": band_label,
        "critical_count": len(triggered_critical),
        "triggered_critical": triggered_critical,
        "observation_date": (
            authored_at.date().isoformat()
            if hasattr(authored_at, "date") else None
        ),
        "loinc_resolution": loinc_code,
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
