"""
format_introspector.py — Structured classification layer above detect_format.

Purpose: every ingest path should UNDERSTAND what it's about to parse
BEFORE it dispatches. detect_format returns a coarse enum; introspect
adds resource-level hints (resourceType, LOINC codes, instrument
mapping, ambiguity score) that let the ingest layer route a payload
to a typed handler instead of always falling through to the LLM
fallback.

Routing recommendations produced here:
    behavioral_screening_ingestor  — QuestionnaireResponse, or Observation
                                     whose LOINC is a known screening
                                     instrument (PHQ-9, GAD-7, AUDIT-C, …)
    labs_ingestor                  — Observation with lab LOINC
    vitals_ingestor                — Observation with vital-sign LOINC
    medications_ingestor           — MedicationRequest / MedicationStatement
    conditions_ingestor            — Condition
    encounters_ingestor            — Encounter
    immunizations_ingestor         — Immunization
    clinical_notes_ingestor        — DiagnosticReport
    procedures_ingestor            — Procedure
    bundle_splitter                — FHIR Bundle
    summary_section_splitter       — HealthEx summary blob
    llm_fallback_normaliser        — used only when ambiguity > threshold
    unknown                        — nothing recognisable

PHI rule: this module NEVER logs raw values — only resource_type,
LOINC/ICD codes, and counts. Raw content is held on the Introspection
payload for the caller, never emitted to logs.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from .format_detector import detect_format, HealthExFormat

log = logging.getLogger(__name__)

# LOINC codes known to be lab observations vs vitals. The lab set is
# intentionally small — we only need to recognise the common panel results
# that HealthEx emits as bare Observation resources. Anything unlabelled
# falls through to the LLM fallback.
_VITAL_LOINCS = {
    "8480-6",   # systolic BP
    "8462-4",   # diastolic BP
    "8867-4",   # heart rate
    "9279-1",   # respiratory rate
    "8310-5",   # body temperature
    "8302-2",   # body height
    "29463-7",  # body weight
    "39156-5",  # BMI
    "2708-6",   # oxygen saturation
    "59408-5",  # SpO2
}

_LAB_LOINCS = {
    "4548-4",   # HbA1c
    "2345-7",   # glucose
    "2093-3",   # cholesterol total
    "13457-7",  # LDL calc
    "2085-9",   # HDL
    "2571-8",   # triglycerides
    "2951-2",   # sodium
    "2823-3",   # potassium
    "2075-0",   # chloride
    "3094-0",   # urea nitrogen
    "2160-0",   # creatinine
    "33914-3", # eGFR
    "1742-6",   # ALT
    "1920-8",   # AST
    "6768-6",   # alkaline phosphatase
    "1975-2",   # bilirubin
    "718-7",    # hemoglobin
    "4544-3",   # hematocrit
    "6690-2",   # WBC
}

ROUTE_BEHAVIORAL_SCREENING = "behavioral_screening_ingestor"
ROUTE_LABS = "labs_ingestor"
ROUTE_VITALS = "vitals_ingestor"
ROUTE_MEDICATIONS = "medications_ingestor"
ROUTE_CONDITIONS = "conditions_ingestor"
ROUTE_ENCOUNTERS = "encounters_ingestor"
ROUTE_IMMUNIZATIONS = "immunizations_ingestor"
ROUTE_CLINICAL_NOTES = "clinical_notes_ingestor"
ROUTE_PROCEDURES = "procedures_ingestor"
ROUTE_BUNDLE_SPLITTER = "bundle_splitter"
ROUTE_SUMMARY = "summary_section_splitter"
ROUTE_LLM_FALLBACK = "llm_fallback_normaliser"
ROUTE_UNKNOWN = "unknown"

# Ambiguity score above which the LLM fallback is allowed to fire.
# Below this, the ingest layer should prefer a typed parser even if
# the payload is less than perfectly tagged.
LLM_FALLBACK_THRESHOLD = 0.7


@dataclass
class Introspection:
    """Structured understanding of a payload.

    Richer than HealthExFormat — includes resource type, LOINC/ICD codes,
    instrument hints, and a routing recommendation that downstream
    dispatchers can use directly.
    """
    format: str
    payload: Any = None
    resource_type_hint: Optional[str] = None
    loinc_codes: list[str] = field(default_factory=list)
    icd10_codes: list[str] = field(default_factory=list)
    rxnorm_codes: list[str] = field(default_factory=list)
    instrument_hint: Optional[str] = None
    record_count_estimate: int = 0
    has_bundle_wrapper: bool = False
    ambiguity_score: float = 1.0
    routing_recommendation: str = ROUTE_UNKNOWN
    warnings: list[str] = field(default_factory=list)


def introspect(
    raw: str,
    resource_type_declared: Optional[str] = None,
) -> Introspection:
    """Inspect a raw payload and emit a structured Introspection.

    Never raises — always returns a valid Introspection (with
    ambiguity_score == 1.0 when nothing could be recognised).
    """
    fmt, payload = detect_format(raw)
    intro = Introspection(format=fmt.value, payload=payload)

    if fmt == HealthExFormat.UNKNOWN or payload is None:
        intro.routing_recommendation = ROUTE_UNKNOWN
        intro.ambiguity_score = 1.0
        intro.warnings.append("detect_format returned UNKNOWN")
        return intro

    # Plain-text summary → HealthEx multi-section blob
    if fmt == HealthExFormat.PLAIN_TEXT_SUMMARY:
        intro.routing_recommendation = ROUTE_SUMMARY
        intro.ambiguity_score = 0.4
        return intro

    if fmt == HealthExFormat.COMPRESSED_TABLE:
        # Compressed table headers disclose the resource type; the
        # existing parser handles sub-type dispatch.
        intro.routing_recommendation = _route_for_declared(resource_type_declared)
        intro.ambiguity_score = 0.5
        return intro

    if fmt == HealthExFormat.FLAT_FHIR_TEXT:
        intro.routing_recommendation = _route_for_declared(resource_type_declared)
        intro.ambiguity_score = 0.5
        return intro

    # FHIR_BUNDLE_JSON — either a genuine Bundle or a single-resource
    # wrapped Bundle (format_detector already did the wrap for us).
    if fmt == HealthExFormat.FHIR_BUNDLE_JSON and isinstance(payload, dict):
        entries = payload.get("entry", []) or []
        intro.has_bundle_wrapper = True
        intro.record_count_estimate = len(entries)

        if len(entries) == 1:
            # Single resource wrapped — dispatch on the inner resourceType
            inner = entries[0].get("resource", {}) if isinstance(entries[0], dict) else {}
            _populate_from_resource(intro, inner, resource_type_declared)
            return intro

        # Multi-entry Bundle — caller should split and re-introspect
        intro.routing_recommendation = ROUTE_BUNDLE_SPLITTER
        intro.ambiguity_score = 0.3
        # Extract code hints across the bundle for forensic audit
        for e in entries:
            res = e.get("resource", {}) if isinstance(e, dict) else {}
            _harvest_codes(intro, res)
        return intro

    if fmt == HealthExFormat.JSON_DICT_ARRAY:
        # Custom JSON wrapper — look for screening hints first.
        if isinstance(payload, dict):
            intro.record_count_estimate = sum(
                len(v) for v in payload.values() if isinstance(v, list)
            )
        intro.routing_recommendation = _route_for_declared(resource_type_declared)
        intro.ambiguity_score = 0.4
        return intro

    # Nothing matched confidently — recommend LLM fallback
    intro.routing_recommendation = ROUTE_LLM_FALLBACK
    intro.ambiguity_score = 0.85
    return intro


def introspect_bundle_entries(bundle_payload: dict) -> list[Introspection]:
    """For a FHIR Bundle, introspect each entry independently.

    Each returned Introspection carries its own routing_recommendation
    so the caller can fan-out the Bundle across typed ingestors.
    """
    out: list[Introspection] = []
    if not isinstance(bundle_payload, dict):
        return out
    entries = bundle_payload.get("entry", []) or []
    for e in entries:
        res = e.get("resource") if isinstance(e, dict) else None
        if not isinstance(res, dict):
            continue
        intro = Introspection(
            format=HealthExFormat.FHIR_BUNDLE_JSON.value,
            payload=res,
            has_bundle_wrapper=False,
        )
        _populate_from_resource(intro, res, None)
        out.append(intro)
    return out


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _route_for_declared(resource_type_declared: Optional[str]) -> str:
    """Map a caller-declared resource_type to a routing_recommendation.

    Used when the payload format is structured but individual entries do
    not self-describe (plain text, compressed table, json dict). The
    caller's declared intent becomes the tie-breaker.
    """
    mapping = {
        "labs":           ROUTE_LABS,
        "vitals":         ROUTE_VITALS,
        "medications":    ROUTE_MEDICATIONS,
        "conditions":     ROUTE_CONDITIONS,
        "encounters":     ROUTE_ENCOUNTERS,
        "immunizations":  ROUTE_IMMUNIZATIONS,
        "notes":          ROUTE_CLINICAL_NOTES,
        "summary":        ROUTE_SUMMARY,
        "screening":      ROUTE_BEHAVIORAL_SCREENING,
    }
    if resource_type_declared and resource_type_declared in mapping:
        return mapping[resource_type_declared]
    return ROUTE_LLM_FALLBACK


def _populate_from_resource(
    intro: Introspection,
    resource: dict,
    resource_type_declared: Optional[str],
) -> None:
    """Fill out the Introspection fields based on a single FHIR resource."""
    rt = resource.get("resourceType", "")
    intro.resource_type_hint = rt or None
    _harvest_codes(intro, resource)

    if rt == "QuestionnaireResponse":
        intro.routing_recommendation = ROUTE_BEHAVIORAL_SCREENING
        intro.ambiguity_score = 0.1
        intro.instrument_hint = _instrument_from_questionnaire(resource)
        return

    if rt == "Observation":
        loinc = _first_loinc(resource)
        if loinc:
            inst = _instrument_from_loinc(loinc)
            if inst:
                intro.routing_recommendation = ROUTE_BEHAVIORAL_SCREENING
                intro.ambiguity_score = 0.1
                intro.instrument_hint = inst
                return
            if loinc in _VITAL_LOINCS:
                intro.routing_recommendation = ROUTE_VITALS
                intro.ambiguity_score = 0.15
                return
            if loinc in _LAB_LOINCS:
                intro.routing_recommendation = ROUTE_LABS
                intro.ambiguity_score = 0.15
                return
        # Unknown LOINC — declare drives the tie-breaker
        intro.routing_recommendation = _route_for_declared(resource_type_declared or "labs")
        intro.ambiguity_score = 0.55
        return

    if rt in ("MedicationRequest", "MedicationStatement"):
        intro.routing_recommendation = ROUTE_MEDICATIONS
        intro.ambiguity_score = 0.1
        return
    if rt == "Condition":
        intro.routing_recommendation = ROUTE_CONDITIONS
        intro.ambiguity_score = 0.1
        return
    if rt == "Encounter":
        intro.routing_recommendation = ROUTE_ENCOUNTERS
        intro.ambiguity_score = 0.1
        return
    if rt == "Immunization":
        intro.routing_recommendation = ROUTE_IMMUNIZATIONS
        intro.ambiguity_score = 0.1
        return
    if rt == "DiagnosticReport":
        intro.routing_recommendation = ROUTE_CLINICAL_NOTES
        intro.ambiguity_score = 0.2
        return
    if rt == "Procedure":
        intro.routing_recommendation = ROUTE_PROCEDURES
        intro.ambiguity_score = 0.1
        return
    if rt == "Patient":
        intro.routing_recommendation = ROUTE_SUMMARY
        intro.ambiguity_score = 0.3
        return

    # Unrecognised FHIR resource type
    intro.routing_recommendation = ROUTE_LLM_FALLBACK
    intro.ambiguity_score = 0.8
    intro.warnings.append(f"unrecognised resourceType={rt!r}")


def _harvest_codes(intro: Introspection, resource: dict) -> None:
    """Pull LOINC/ICD-10/RxNorm codes out of a resource for forensics."""
    codings = _coding_list(resource.get("code", {}))
    for c in codings:
        sys = (c.get("system") or "").lower()
        code = c.get("code")
        if not code:
            continue
        if "loinc" in sys and code not in intro.loinc_codes:
            intro.loinc_codes.append(code)
        elif "icd" in sys and code not in intro.icd10_codes:
            intro.icd10_codes.append(code)
        elif "rxnorm" in sys and code not in intro.rxnorm_codes:
            intro.rxnorm_codes.append(code)

    med = resource.get("medicationCodeableConcept", {})
    for c in _coding_list(med):
        sys = (c.get("system") or "").lower()
        code = c.get("code")
        if code and "rxnorm" in sys and code not in intro.rxnorm_codes:
            intro.rxnorm_codes.append(code)


def _coding_list(concept: Any) -> list[dict]:
    if isinstance(concept, dict):
        c = concept.get("coding")
        return c if isinstance(c, list) else []
    return []


def _first_loinc(resource: dict) -> Optional[str]:
    for c in _coding_list(resource.get("code", {})):
        sys = (c.get("system") or "").lower()
        if "loinc" in sys and c.get("code"):
            return c["code"]
    return None


def _instrument_from_loinc(loinc: str) -> Optional[str]:
    """Map a LOINC to an instrument_key via screening_registry."""
    try:
        from pathlib import Path as _P
        import sys as _sys
        _skills = _P(__file__).resolve().parent.parent.parent.parent / "mcp-server"
        if str(_skills) not in _sys.path:
            _sys.path.insert(0, str(_skills))
        from skills.screening_registry import get_instrument_for_loinc
    except Exception as exc:
        log.debug("screening_registry unavailable: %s", exc)
        return None
    inst = get_instrument_for_loinc(loinc)
    return inst.key if inst else None


def _instrument_from_questionnaire(resource: dict) -> Optional[str]:
    """Resolve a QuestionnaireResponse's instrument via its questionnaire URL.

    Falls back to keyword lookup so the free-text questionnaire URI
    'http://loinc.org/vs/44249-1' maps to 'phq9'.
    """
    q_url = resource.get("questionnaire") or ""
    if not q_url:
        return None
    # Embedded LOINC code in the URL
    import re as _re
    loinc_match = _re.search(r"(\d{4,5}-\d)", q_url)
    if loinc_match:
        inst = _instrument_from_loinc(loinc_match.group(1))
        if inst:
            return inst
    try:
        from pathlib import Path as _P
        import sys as _sys
        _skills = _P(__file__).resolve().parent.parent.parent.parent / "mcp-server"
        if str(_skills) not in _sys.path:
            _sys.path.insert(0, str(_skills))
        from skills.screening_registry import get_instrument_by_keyword
        inst = get_instrument_by_keyword(q_url)
        return inst.key if inst else None
    except Exception:
        return None
