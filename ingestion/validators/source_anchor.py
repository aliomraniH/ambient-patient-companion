"""
SOURCE ANCHOR VALIDATOR
=======================
Purpose: Verify that every numeric value the LLM extraction plan claims to have
found actually appears verbatim in the source blob. Catches fabricated numerics
at zero additional API cost.

Root cause addressed: LLM extraction planner can hallucinate common clinical
values (e.g., creatinine 1.5) even when absent from source. These enter Bronze
as immutable truth and corrupt all downstream reasoning.

Insertion point: Call verify_extracted_numerics() AFTER Phase 2 execution,
BEFORE writing any record to raw_fhir_cache.
"""

import logging
import re
from typing import Any

log = logging.getLogger(__name__)

# Fields that contain clinical numerics requiring source anchoring
NUMERIC_FIELDS = {
    # Lab values
    "value_quantity", "value", "result_value", "numeric_value",
    "result_numeric",
    # Vitals
    "systolic", "diastolic", "heart_rate", "temperature", "weight", "height", "bmi",
    # Dosing
    "dose_quantity", "dose_value", "frequency_value",
}

# Fields exempt from anchoring (identifiers, codes, timestamps)
EXEMPT_FIELDS = {
    "loinc_code", "rxnorm_code", "snomed_code", "patient_mrn",
    "encounter_id", "resource_id", "version", "patient_id",
    "code", "icd10", "icd10_code",
}


def verify_extracted_numerics(
    source_blob: str,
    extracted: dict[str, Any],
    resource_type: str = "unknown",
) -> dict[str, Any]:
    """
    Cross-reference every numeric extracted value against the source blob.

    Returns:
        {
            "verified": {field: value},     - Values confirmed in source
            "nulled": {field: None},        - Replaced with None (not in source)
            "flags": [{field, value, status}], - Audit trail of every decision
            "anchor_rate": float,           - 0.0-1.0; alert if < 0.95
            "numeric_fields_checked": int,
            "anchored_count": int,
        }

    NEVER silently discard: nulled values are stored with a quality_flag so
    downstream agents know the data is missing, not absent.
    """
    if not source_blob or not extracted:
        return {
            "verified": extracted or {},
            "nulled": {},
            "flags": [],
            "anchor_rate": 1.0,
            "numeric_fields_checked": 0,
            "anchored_count": 0,
        }

    verified: dict[str, Any] = {}
    nulled: dict[str, Any] = {}
    flags: list[dict] = []
    numeric_count = 0
    anchored_count = 0

    for field, value in extracted.items():
        # Skip exempt fields
        if field in EXEMPT_FIELDS:
            verified[field] = value
            continue

        # Only anchor fields we know contain numerics, or try to anchor any numeric value
        if field not in NUMERIC_FIELDS:
            # Check if the value is numeric anyway
            if value is not None and _is_numeric(value):
                # Treat as numeric field
                pass
            else:
                verified[field] = value
                continue

        if value is None:
            verified[field] = None
            continue

        numeric_count += 1
        str_val = str(value).strip()

        # Strategy 1: Exact string match
        if str_val in source_blob:
            verified[field] = value
            anchored_count += 1
            flags.append({"field": field, "value": value, "status": "anchored_exact"})
            continue

        # Strategy 2: Numeric equivalence (handles "1.50" == "1.5", "7.40" == "7.4")
        try:
            num = float(str_val)
            if _find_numeric_in_blob(num, source_blob):
                verified[field] = value
                anchored_count += 1
                flags.append({"field": field, "value": value, "status": "anchored_numeric_equiv"})
                continue
        except (ValueError, TypeError):
            pass

        # Strategy 3: Not found — null and flag
        nulled[field] = value  # Store original value for audit
        verified[field] = None
        flags.append({
            "field": field,
            "value": value,
            "status": "unanchored_hallucination_risk",
            "resource_type": resource_type,
            "action": "nulled_not_discarded",
        })

    anchor_rate = anchored_count / numeric_count if numeric_count > 0 else 1.0

    return {
        "verified": verified,
        "nulled": nulled,
        "flags": flags,
        "anchor_rate": anchor_rate,
        "numeric_fields_checked": numeric_count,
        "anchored_count": anchored_count,
    }


def assert_anchor_rate(result: dict, threshold: float = 0.95, patient_mrn: str = "") -> None:
    """Log a warning if anchor rate falls below threshold."""
    rate = result.get("anchor_rate", 1.0)
    if rate < threshold and result.get("numeric_fields_checked", 0) > 0:
        log.warning(
            "[SOURCE_ANCHOR] Low anchor rate %.2f%% for MRN %s. "
            "Nulled fields: %s. Review extraction plan quality for this blob.",
            rate * 100,
            patient_mrn,
            list(result["nulled"].keys()),
        )


def _is_numeric(value: Any) -> bool:
    """Check if a value is numeric (int or float or numeric string)."""
    if isinstance(value, (int, float)):
        return True
    try:
        float(str(value))
        return True
    except (ValueError, TypeError):
        return False


def _find_numeric_in_blob(num: float, blob: str) -> bool:
    """Search for any common representation of a number in the blob."""
    # Try integer representation
    if num == int(num):
        if str(int(num)) in blob:
            return True

    # Try various decimal representations
    representations = set()
    representations.add(f"{num:.0f}")
    representations.add(f"{num:.1f}")
    representations.add(f"{num:.2f}")
    representations.add(f"{num:.3f}")
    representations.add(str(num))

    # Remove trailing zeros for matching (7.40 -> 7.4)
    stripped = f"{num:g}"
    representations.add(stripped)

    for rep in representations:
        # Use word boundary to avoid matching substrings (e.g., "14" in "140")
        pattern = rf'(?<!\d){re.escape(rep)}(?!\d)'
        if re.search(pattern, blob):
            return True

    return False
