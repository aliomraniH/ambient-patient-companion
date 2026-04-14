"""Behavioral + SDoH screening ingestor — LOINC-keyed, item-aware.

Accepts FHIR `Observation` and `QuestionnaireResponse` resources, detects
the screening instrument via the screening_registry / sdoh_registry LOINC
maps, extracts per-item answers and scores, and writes structured rows to
`behavioral_screenings` / `sdoh_screenings`.

PHI note: this module is called from the ingestion pipeline — it touches
DB only. It does not log patient identifiers or answer values.

Design rules:
  - Returns None on unknown LOINC. Never raises.
  - Item-level answers are the source of truth; totals are recomputed
    from items when possible, otherwise taken from Observation.valueQuantity.
  - Idempotent via UNIQUE (patient_id, instrument_key, observation_date).
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import date, datetime
from typing import Optional

from skills.screening_registry import (
    SCREENING_REGISTRY,
    LOINC_TO_INSTRUMENT,
    LOINC_ITEM_TO_INSTRUMENT,
    ScreeningInstrument,
    get_instrument_by_loinc,
    get_instrument_by_key,
    severity_band_for_score,
    is_positive_screen,
    critical_items_triggered,
)
from skills.sdoh_registry import (
    SDOH_REGISTRY,
    SDOH_LOINC_TO_INSTRUMENT,
    SdoHInstrument,
    evaluate_sdoh_positive_domains,
    get_sdoh_instrument_by_loinc,
    get_sdoh_instrument_by_key,
)

logging.basicConfig(level=logging.INFO, stream=sys.stderr)
logger = logging.getLogger(__name__)


# ── Helpers ─────────────────────────────────────────────────────────────

def _parse_date(val) -> Optional[date]:
    """Best-effort FHIR datetime → date."""
    if val is None:
        return None
    if isinstance(val, date) and not isinstance(val, datetime):
        return val
    if isinstance(val, datetime):
        return val.date()
    s = str(val).strip()
    if not s:
        return None
    # Try ISO date / datetime variants.
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%SZ",
                "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S.%fZ"):
        try:
            return datetime.strptime(s[:26] if "." in s else s[:19],
                                      fmt.replace("Z", "")).date()
        except ValueError:
            continue
    # Last resort: truncate to first 10 chars.
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        return None


def _extract_resource_loincs(resource: dict) -> list[str]:
    """Pull LOINC codes out of Observation.code or QuestionnaireResponse
    top-level `code` / linked Questionnaire reference. Returns a list so
    panels carrying multiple codings can be matched in priority order.
    """
    loincs: list[str] = []
    for coding in (resource.get("code", {}) or {}).get("coding", []) or []:
        system = (coding.get("system") or "").lower()
        if "loinc" in system or coding.get("system") == "http://loinc.org":
            code = coding.get("code")
            if code:
                loincs.append(str(code))
    # QuestionnaireResponse may carry its instrument ID via
    # `questionnaire` (canonical URL) — not a LOINC but we return as-is
    # for the caller to fall back to keyword matching.
    q = resource.get("questionnaire")
    if isinstance(q, str) and "loinc" in q.lower():
        # e.g. "http://loinc.org/44249-1"
        tail = q.rstrip("/").split("/")[-1]
        if tail:
            loincs.append(tail)
    return loincs


def _score_from_answer(answer: dict) -> tuple[Optional[int], str]:
    """Given one FHIR answer dict, return (numeric_score, raw_value_str).

    FHIR answer value types we handle: valueInteger, valueDecimal,
    valueCoding (extract .code), valueString, valueBoolean.
    """
    raw: str = ""
    score: Optional[int] = None
    if "valueInteger" in answer:
        try:
            score = int(answer["valueInteger"])
            raw = str(score)
        except (TypeError, ValueError):
            pass
    elif "valueDecimal" in answer:
        try:
            score = int(float(answer["valueDecimal"]))
            raw = str(answer["valueDecimal"])
        except (TypeError, ValueError):
            pass
    elif "valueCoding" in answer:
        coding = answer["valueCoding"] or {}
        raw = str(coding.get("code") or coding.get("display") or "")
        # Some instruments encode 0-3 scores as the coding `code`.
        try:
            score = int(raw)
        except (TypeError, ValueError):
            pass
    elif "valueString" in answer:
        raw = str(answer["valueString"])
        try:
            score = int(raw)
        except (TypeError, ValueError):
            pass
    elif "valueBoolean" in answer:
        raw = str(answer["valueBoolean"]).lower()
        score = 1 if answer["valueBoolean"] else 0
    return score, raw


def _item_number_for_linkid(
    linkid: str,
    prefix: str,
    candidates_map: dict[str, int] | None = None,
) -> Optional[int]:
    """Derive a 1-based item number from a QuestionnaireResponse linkId.

    Strategy: strip the registry prefix and parse the trailing integer.
    Fall back to a candidates map (exact-match linkId → item_number) for
    non-standard instrument linkIds.
    """
    if not linkid:
        return None
    if candidates_map and linkid in candidates_map:
        return candidates_map[linkid]
    if prefix and linkid.startswith(prefix):
        tail = linkid[len(prefix):]
        try:
            return int(tail)
        except ValueError:
            return None
    # Last resort: pick the last integer in the string.
    digits = "".join(ch if ch.isdigit() else " " for ch in linkid).split()
    if digits:
        try:
            return int(digits[-1])
        except ValueError:
            return None
    return None


# ── Behavioral screening parsing ────────────────────────────────────────

def _iter_qr_items(qr: dict):
    """Depth-first walk over QuestionnaireResponse.item[] yielding
    (linkId, answer_dict) pairs. Flattens nested groups.
    """
    stack = list(qr.get("item", []) or [])
    while stack:
        node = stack.pop(0)
        if not isinstance(node, dict):
            continue
        linkid = node.get("linkId") or ""
        for ans in node.get("answer", []) or []:
            yield linkid, ans
        # Recurse into nested items.
        for child in node.get("item", []) or []:
            stack.append(child)


def parse_questionnaire_response_to_screening(
    qr: dict,
    patient_id: str,
) -> Optional[dict]:
    """Parse a FHIR QuestionnaireResponse into a behavioral_screenings row.

    Returns None when the panel LOINC doesn't map to any registry entry.
    """
    if not isinstance(qr, dict) or qr.get("resourceType") != "QuestionnaireResponse":
        return None

    loincs = _extract_resource_loincs(qr)
    instrument: Optional[ScreeningInstrument] = None
    for code in loincs:
        instrument = get_instrument_by_loinc(code)
        if instrument:
            break
    if instrument is None:
        return None

    observed = _parse_date(qr.get("authored") or qr.get("meta", {}).get("lastUpdated"))
    if observed is None:
        observed = date.today()

    # Build per-item scores + answers from .item[].
    item_scores: dict[int, int] = {}
    item_answers: dict[int, str] = {}
    loinc_item_candidates = {
        code: idx
        for code, (key, idx) in LOINC_ITEM_TO_INSTRUMENT.items()
        if key == instrument.key
    }
    for linkid, ans in _iter_qr_items(qr):
        num = _item_number_for_linkid(linkid, instrument.linkid_prefix)
        if num is None:
            # Some payloads use the LOINC item code as the linkId.
            hit = loinc_item_candidates.get(linkid)
            if hit:
                num = hit
        if num is None:
            continue
        score, raw = _score_from_answer(ans)
        if score is not None:
            item_scores[num] = score
        if raw:
            item_answers[num] = raw

    # Total score: prefer sum of known item_scores if covering all items,
    # otherwise fall back to an explicit valueQuantity/valueInteger at
    # the resource root.
    total: Optional[int] = None
    expected_items = len(instrument.loinc_item_codes) or None
    if item_scores and (expected_items is None or len(item_scores) >= expected_items):
        total = sum(item_scores.values())

    return _build_screening_row(
        instrument=instrument,
        patient_id=patient_id,
        observed=observed,
        total=total,
        item_scores=item_scores,
        item_answers=item_answers,
        fhir_resource=qr,
        fhir_resource_type="QuestionnaireResponse",
    )


def parse_fhir_observation_to_screening(
    obs: dict,
    patient_id: str,
) -> Optional[dict]:
    """Parse a FHIR Observation panel into a behavioral_screenings row."""
    if not isinstance(obs, dict) or obs.get("resourceType") != "Observation":
        return None

    loincs = _extract_resource_loincs(obs)
    instrument: Optional[ScreeningInstrument] = None
    for code in loincs:
        instrument = get_instrument_by_loinc(code)
        if instrument:
            break
    if instrument is None:
        return None

    observed = _parse_date(
        obs.get("effectiveDateTime")
        or obs.get("effectivePeriod", {}).get("start")
        or obs.get("issued")
    ) or date.today()

    # Total score via valueQuantity / valueInteger.
    total: Optional[int] = None
    vq = obs.get("valueQuantity") or {}
    if "value" in vq:
        try:
            total = int(float(vq["value"]))
        except (TypeError, ValueError):
            pass
    if total is None and "valueInteger" in obs:
        try:
            total = int(obs["valueInteger"])
        except (TypeError, ValueError):
            pass

    # Per-item scores via .component[] keyed by item-level LOINC.
    item_scores: dict[int, int] = {}
    item_answers: dict[int, str] = {}
    for comp in obs.get("component", []) or []:
        comp_loincs: list[str] = []
        for coding in (comp.get("code", {}) or {}).get("coding", []) or []:
            if coding.get("code"):
                comp_loincs.append(str(coding["code"]))
        item_num: Optional[int] = None
        for c in comp_loincs:
            hit = LOINC_ITEM_TO_INSTRUMENT.get(c)
            if hit and hit[0] == instrument.key:
                item_num = hit[1]
                break
        if item_num is None:
            continue
        score, raw = _score_from_answer(comp)
        if score is not None:
            item_scores[item_num] = score
        if raw:
            item_answers[item_num] = raw

    return _build_screening_row(
        instrument=instrument,
        patient_id=patient_id,
        observed=observed,
        total=total,
        item_scores=item_scores,
        item_answers=item_answers,
        fhir_resource=obs,
        fhir_resource_type="Observation",
    )


def _build_screening_row(
    instrument: ScreeningInstrument,
    patient_id: str,
    observed: date,
    total: Optional[int],
    item_scores: dict[int, int],
    item_answers: dict[int, str],
    fhir_resource: dict,
    fhir_resource_type: str,
) -> dict:
    """Common field builder for both Observation and QR parse paths."""
    band = severity_band_for_score(instrument, total) if total is not None else None
    positive = is_positive_screen(instrument, total) if total is not None else None
    triggered = critical_items_triggered(instrument, item_scores)

    return {
        "patient_id": patient_id,
        "instrument_key": instrument.key,
        "instrument_name": instrument.display_name,
        "domain": instrument.domain,
        "observation_date": observed,
        "total_score": total,
        "severity_band": band.label if band else None,
        "is_positive": positive,
        "item_scores": {str(k): v for k, v in item_scores.items()},
        "item_answers": {str(k): v for k, v in item_answers.items()},
        "triggered_critical": triggered,
        "source": "fhir",
        "fhir_resource_type": fhir_resource_type,
        "fhir_resource_id": str(fhir_resource.get("id") or ""),
        "raw_payload": fhir_resource,
    }


async def insert_screening(conn, row: dict) -> Optional[str]:
    """Insert a behavioral_screenings row. Idempotent. Returns the row id
    on insert/update; None on DB failure.
    """
    if not row:
        return None
    try:
        result = await conn.fetchrow(
            """
            INSERT INTO behavioral_screenings
                (patient_id, instrument_key, instrument_name, domain,
                 observation_date, total_score, severity_band, is_positive,
                 item_scores, item_answers, triggered_critical,
                 source, fhir_resource_type, fhir_resource_id, raw_payload)
            VALUES
                ($1::uuid, $2, $3, $4, $5, $6, $7, $8,
                 $9::jsonb, $10::jsonb, $11::jsonb,
                 $12, $13, $14, $15::jsonb)
            ON CONFLICT (patient_id, instrument_key, observation_date)
            DO UPDATE SET
                total_score        = EXCLUDED.total_score,
                severity_band      = EXCLUDED.severity_band,
                is_positive        = EXCLUDED.is_positive,
                item_scores        = EXCLUDED.item_scores,
                item_answers       = EXCLUDED.item_answers,
                triggered_critical = EXCLUDED.triggered_critical,
                source             = EXCLUDED.source,
                fhir_resource_type = EXCLUDED.fhir_resource_type,
                fhir_resource_id   = EXCLUDED.fhir_resource_id,
                raw_payload        = EXCLUDED.raw_payload
            RETURNING id
            """,
            row["patient_id"], row["instrument_key"], row["instrument_name"],
            row["domain"], row["observation_date"], row.get("total_score"),
            row.get("severity_band"), row.get("is_positive"),
            json.dumps(row.get("item_scores") or {}),
            json.dumps(row.get("item_answers") or {}),
            json.dumps(row.get("triggered_critical") or []),
            row.get("source"), row.get("fhir_resource_type"),
            row.get("fhir_resource_id"),
            json.dumps(row.get("raw_payload") or {}),
        )
        return str(result["id"]) if result else None
    except Exception as e:
        logger.warning("insert_screening failed: %s", type(e).__name__)
        return None


# ── SDoH parsing ────────────────────────────────────────────────────────

def parse_questionnaire_response_to_sdoh(
    qr: dict,
    patient_id: str,
) -> Optional[dict]:
    if not isinstance(qr, dict) or qr.get("resourceType") != "QuestionnaireResponse":
        return None

    loincs = _extract_resource_loincs(qr)
    instrument: Optional[SdoHInstrument] = None
    for code in loincs:
        instrument = get_sdoh_instrument_by_loinc(code)
        if instrument:
            break
    if instrument is None:
        return None

    observed = _parse_date(qr.get("authored")) or date.today()

    item_answers: dict[int, str] = {}
    item_scores: dict[int, int] = {}
    linkid_candidate_map: dict[str, int] = {}
    for item in instrument.items:
        for cand in item.linkid_candidates:
            linkid_candidate_map[cand] = item.item_number

    for linkid, ans in _iter_qr_items(qr):
        num = _item_number_for_linkid(linkid, instrument.linkid_prefix,
                                       candidates_map=linkid_candidate_map)
        if num is None:
            continue
        score, raw = _score_from_answer(ans)
        if score is not None:
            item_scores[num] = score
        if raw:
            item_answers[num] = raw

    positive_domains = evaluate_sdoh_positive_domains(instrument, item_answers)

    return {
        "patient_id": patient_id,
        "instrument_key": instrument.key,
        "instrument_name": instrument.display_name,
        "observation_date": observed,
        "positive_domains": positive_domains,
        "item_scores": {str(k): v for k, v in item_scores.items()},
        "item_answers": {str(k): v for k, v in item_answers.items()},
        "source": "fhir",
        "fhir_resource_type": "QuestionnaireResponse",
        "fhir_resource_id": str(qr.get("id") or ""),
        "raw_payload": qr,
    }


async def insert_sdoh_screening(conn, row: dict) -> Optional[str]:
    if not row:
        return None
    try:
        result = await conn.fetchrow(
            """
            INSERT INTO sdoh_screenings
                (patient_id, instrument_key, instrument_name,
                 observation_date, positive_domains,
                 item_answers, item_scores, source,
                 fhir_resource_type, fhir_resource_id, raw_payload)
            VALUES
                ($1::uuid, $2, $3, $4, $5,
                 $6::jsonb, $7::jsonb,
                 $8, $9, $10, $11::jsonb)
            ON CONFLICT (patient_id, instrument_key, observation_date)
            DO UPDATE SET
                positive_domains   = EXCLUDED.positive_domains,
                item_answers       = EXCLUDED.item_answers,
                item_scores        = EXCLUDED.item_scores,
                source             = EXCLUDED.source,
                fhir_resource_type = EXCLUDED.fhir_resource_type,
                fhir_resource_id   = EXCLUDED.fhir_resource_id,
                raw_payload        = EXCLUDED.raw_payload
            RETURNING id
            """,
            row["patient_id"], row["instrument_key"], row["instrument_name"],
            row["observation_date"], row.get("positive_domains") or [],
            json.dumps(row.get("item_answers") or {}),
            json.dumps(row.get("item_scores") or {}),
            row.get("source"), row.get("fhir_resource_type"),
            row.get("fhir_resource_id"),
            json.dumps(row.get("raw_payload") or {}),
        )
        return str(result["id"]) if result else None
    except Exception as e:
        logger.warning("insert_sdoh_screening failed: %s", type(e).__name__)
        return None


# ── Unified dispatch ────────────────────────────────────────────────────

async def ingest_fhir_resource(
    conn,
    resource: dict,
    patient_id: str,
) -> dict:
    """Dispatch one FHIR resource through the behavioral and SDoH ingestors.

    Returns a summary dict: `{behavioral_screening_id, sdoh_screening_id,
    instrument_key, sdoh_instrument_key}`. Unknown resource types /
    unmapped LOINCs → empty dict.
    """
    if not isinstance(resource, dict):
        return {}
    rtype = resource.get("resourceType")
    result: dict = {}
    if rtype == "QuestionnaireResponse":
        row = parse_questionnaire_response_to_screening(resource, patient_id)
        if row:
            row_id = await insert_screening(conn, row)
            result["behavioral_screening_id"] = row_id
            result["instrument_key"] = row["instrument_key"]
            result["domain"] = row["domain"]
            result["observation_date"] = row["observation_date"].isoformat()
        else:
            sdoh_row = parse_questionnaire_response_to_sdoh(resource, patient_id)
            if sdoh_row:
                sid = await insert_sdoh_screening(conn, sdoh_row)
                result["sdoh_screening_id"] = sid
                result["sdoh_instrument_key"] = sdoh_row["instrument_key"]
                result["observation_date"] = sdoh_row["observation_date"].isoformat()
    elif rtype == "Observation":
        row = parse_fhir_observation_to_screening(resource, patient_id)
        if row:
            row_id = await insert_screening(conn, row)
            result["behavioral_screening_id"] = row_id
            result["instrument_key"] = row["instrument_key"]
            result["domain"] = row["domain"]
            result["observation_date"] = row["observation_date"].isoformat()
    return result


def register(mcp):  # pragma: no cover — library, not a tool
    return
