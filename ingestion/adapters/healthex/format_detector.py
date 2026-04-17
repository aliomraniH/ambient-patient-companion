"""
format_detector.py — Detect which of the known HealthEx payload formats
a raw string belongs to and return the appropriate enum + parsed payload.

HealthEx MCP tools return data in at least four distinct formats:
  A. Plain text summary (from get_health_summary)
  B. Compressed dictionary table (from get_conditions, get_medications, etc.)
  C. Flat FHIR text with "key is value" sentences (from search tool)
  D. Proper FHIR R4 Bundle JSON (from FHIR server / structured ingest calls)

Plus a fifth case for custom JSON dict-with-arrays that Claude constructs.
"""
import json
from enum import Enum


class HealthExFormat(Enum):
    PLAIN_TEXT_SUMMARY = "plain_text_summary"   # Format A
    COMPRESSED_TABLE = "compressed_table"        # Format B
    FLAT_FHIR_TEXT = "flat_fhir_text"            # Format C
    FHIR_BUNDLE_JSON = "fhir_bundle_json"        # Format D
    JSON_DICT_ARRAY = "json_dict_array"          # Custom JSON with arrays
    UNKNOWN = "unknown"


_KNOWN_ARRAY_KEYS = {
    "conditions", "medications", "labs", "visits",
    "encounters", "immunizations", "labResults", "lab_results",
    "observations", "drugs", "prescriptions", "problems",
    "diagnoses", "appointments",
}


def detect_format(raw: str) -> tuple[HealthExFormat, object]:
    """
    Detect HealthEx payload format and return (format_enum, parsed_payload).
    Never raises on valid input — always returns a valid enum and best-effort
    payload.
    """
    if not isinstance(raw, str) or not raw.strip():
        return HealthExFormat.UNKNOWN, None

    stripped = raw.strip()

    # Format A: Plain text summary starting with "PATIENT:"
    if stripped.upper().startswith("PATIENT:"):
        return HealthExFormat.PLAIN_TEXT_SUMMARY, stripped

    # Format B: Compressed dictionary table starting with "#" and pipe-delimited
    if stripped.startswith("#") and "|" in stripped:
        return HealthExFormat.COMPRESSED_TABLE, stripped

    # Format C: Flat FHIR text (key=value sentences)
    if stripped.startswith("resourceType is "):
        return HealthExFormat.FLAT_FHIR_TEXT, stripped

    # Try JSON parse
    try:
        payload = json.loads(stripped)
    except (json.JSONDecodeError, ValueError):
        return HealthExFormat.UNKNOWN, stripped

    # Guard: JSON string (double-encoded) — recurse once to unwrap
    if isinstance(payload, str):
        return detect_format(payload)

    # JSON list — could be array of FHIR resources or native dicts
    if isinstance(payload, list):
        if payload and isinstance(payload[0], dict):
            if payload[0].get("resourceType") == "Bundle":
                return HealthExFormat.FHIR_BUNDLE_JSON, payload[0]
            return HealthExFormat.JSON_DICT_ARRAY, {"_items": payload}
        return HealthExFormat.UNKNOWN, payload

    if not isinstance(payload, dict):
        return HealthExFormat.UNKNOWN, payload

    # Format D: FHIR Bundle
    if payload.get("resourceType") == "Bundle" and "entry" in payload:
        return HealthExFormat.FHIR_BUNDLE_JSON, payload

    # Single FHIR resource (Observation, Condition, etc.)
    # QuestionnaireResponse was previously missing here — a bare PHQ-9 QR
    # fell through to UNKNOWN and the typed screening ingestor was never
    # reached, causing silent behavioral-screening drops.
    if payload.get("resourceType") in (
        "Observation", "Condition", "MedicationRequest",
        "MedicationStatement", "Encounter", "Immunization", "Patient",
        "QuestionnaireResponse", "DiagnosticReport", "Procedure",
        "AllergyIntolerance",
    ):
        return HealthExFormat.FHIR_BUNDLE_JSON, {
            "resourceType": "Bundle",
            "type": "searchset",
            "entry": [{"resource": payload}],
        }

    # JSON dict with known array keys
    if any(k in payload for k in _KNOWN_ARRAY_KEYS):
        return HealthExFormat.JSON_DICT_ARRAY, payload

    return HealthExFormat.UNKNOWN, payload
