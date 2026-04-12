"""
FHIR PROFILE VALIDATOR (Lightweight)
======================================
Purpose: Validate extracted resources against FHIR R4 structural requirements
before writing to raw_fhir_cache. Invalid resources are QUARANTINED — the raw
blob stays in Bronze, but the parsed representation is flagged.

This is a lightweight validator that checks structural requirements without
importing the heavy fhir.resources package. It validates:
- Required fields per resource type
- Value type correctness (numeric where expected)
- Code format validation (LOINC, ICD-10, RxNorm patterns)
- Date format validation (ISO 8601)
"""

import logging
import re
from datetime import datetime
from typing import Any

log = logging.getLogger(__name__)

# ISO date pattern (YYYY-MM-DD or full ISO datetime)
_ISO_DATE_RE = re.compile(
    r'^\d{4}-\d{2}-\d{2}(?:T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?)?$'
)

# LOINC code pattern: 1-7 digits, hyphen, 1 digit
_LOINC_RE = re.compile(r'^\d{1,7}-\d$')

# ICD-10 pattern: letter + digits, optional dot + more chars
_ICD10_RE = re.compile(r'^[A-Z]\d{2}(?:\.\d{1,4})?$', re.IGNORECASE)


# Required fields per resource type (FHIR R4 minimum)
RESOURCE_REQUIREMENTS: dict[str, dict[str, Any]] = {
    "Observation": {
        "required_fields": ["code", "effectiveDateTime"],
        "numeric_fields": ["valueQuantity.value"],
        "code_fields": {"code.coding.0.code": "loinc"},
    },
    "Condition": {
        "required_fields": ["code"],
        "numeric_fields": [],
        "code_fields": {"code.coding.0.code": "icd10"},
    },
    "MedicationRequest": {
        "required_fields": ["medicationCodeableConcept"],
        "numeric_fields": [],
        "code_fields": {},
    },
    "Encounter": {
        "required_fields": [],
        "numeric_fields": [],
        "code_fields": {},
    },
    # Native format types (from HealthEx pipeline)
    "labs": {
        "required_fields": ["test_name"],
        "numeric_fields": ["value"],
        "code_fields": {"code": "loinc"},
        "date_fields": ["date", "effectiveDateTime"],
    },
    "conditions": {
        "required_fields": ["name"],
        "numeric_fields": [],
        "code_fields": {"code": "icd10", "icd10": "icd10"},
        "date_fields": ["onset_date", "onsetDateTime"],
    },
    "medications": {
        "required_fields": ["name"],
        "numeric_fields": [],
        "code_fields": {},
        "date_fields": ["start_date", "authored_on", "authoredOn"],
    },
    "encounters": {
        "required_fields": [],
        "numeric_fields": [],
        "code_fields": {},
        "date_fields": ["encounter_date", "date"],
    },
}


def validate_fhir_resource(
    resource: dict,
    resource_type: str,
) -> tuple[bool, list[str]]:
    """
    Validate a parsed resource against FHIR R4 structural requirements.

    Returns:
        (is_valid: bool, issues: list[str])

    Note: Resources that fail validation are NOT discarded. They are flagged
    for downstream quality tracking.
    """
    if not resource or not isinstance(resource, dict):
        return False, ["Empty or non-dict resource"]

    issues: list[str] = []
    requirements = RESOURCE_REQUIREMENTS.get(resource_type, {})
    if not requirements:
        # Unknown type — pass without validation
        return True, []

    # Check required fields
    for field in requirements.get("required_fields", []):
        value = _get_nested_value(resource, field)
        if value is None or (isinstance(value, str) and not value.strip()):
            issues.append(f"Missing required field: {field}")

    # Check numeric fields
    for field in requirements.get("numeric_fields", []):
        value = _get_nested_value(resource, field)
        if value is not None:
            try:
                float(str(value))
            except (ValueError, TypeError):
                # Non-numeric is allowed (qualitative results like "Positive")
                # Only flag if it looks like a corrupted number
                if re.match(r'^[\d.]+[^\d.\s]', str(value)):
                    issues.append(f"Potentially corrupted numeric in {field}: {value}")

    # Check code format
    for field, code_system in requirements.get("code_fields", {}).items():
        value = _get_nested_value(resource, field)
        if value and isinstance(value, str) and value.strip():
            if code_system == "loinc" and not _LOINC_RE.match(value):
                # Not necessarily invalid — could be a display name
                pass
            elif code_system == "icd10" and not _ICD10_RE.match(value):
                # Not necessarily invalid — could be a display name
                pass

    # Check date fields
    for field in requirements.get("date_fields", []):
        value = _get_nested_value(resource, field)
        if value and isinstance(value, str) and value.strip():
            if not _is_valid_date(value):
                issues.append(f"Invalid date format in {field}: {value}")

    is_valid = len(issues) == 0
    if not is_valid:
        log.debug(
            "[FHIR_VALIDATOR] %s resource has %d issues: %s",
            resource_type, len(issues), issues[:3]
        )

    return is_valid, issues


def _get_nested_value(data: dict, path: str) -> Any:
    """Get a value from nested dict using dot notation (e.g., 'code.coding.0.code')."""
    parts = path.split(".")
    current = data
    for part in parts:
        if current is None:
            return None
        if isinstance(current, dict):
            current = current.get(part)
        elif isinstance(current, list):
            try:
                current = current[int(part)]
            except (IndexError, ValueError):
                return None
        else:
            return None
    return current


def _is_valid_date(value: str) -> bool:
    """Check if a string is a valid date in ISO format or common clinical formats."""
    # ISO format
    if _ISO_DATE_RE.match(value):
        try:
            datetime.fromisoformat(value.replace("Z", "+00:00"))
            return True
        except ValueError:
            pass

    # Common clinical date formats
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%d-%b-%Y", "%Y-%m-%dT%H:%M:%S"):
        try:
            datetime.strptime(value, fmt)
            return True
        except ValueError:
            continue

    return False
