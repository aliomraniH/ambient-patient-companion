"""
CLINICAL PLAUSIBILITY VALIDATOR — Bronze to Silver Gate
=======================================================
Validates extracted values against clinically-defined acceptable ranges.
Keyed to LOINC codes for precision — the same value (e.g., 7.4) means
different things for different tests.

IMPORTANT: Implausible values are FLAGGED, never silently discarded.
Flagged records reach Silver with quality_status = "flagged" and
quality_flags populated. Agents see the flag and can route to gap detection.
"""

import logging
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger(__name__)


# LOINC code -> (min, max, unit, guideline_source)
# These are PHYSIOLOGICAL limits (not reference ranges) — values outside
# these ranges indicate extraction errors, not clinical outliers.
LOINC_PLAUSIBILITY: dict[str, tuple[float, float, str, str]] = {
    # Metabolic / Diabetes
    "4548-4":  (2.0,   20.0,  "%",       "ADA Standards 2024 — HbA1c"),
    "2345-7":  (20.0,  2000.0, "mg/dL",  "ADA — fasting glucose"),
    "2160-0":  (0.1,   30.0,  "mg/dL",   "KDIGO — serum creatinine"),
    "14959-1": (0.0,   10000.0, "mg/g",  "ADA — UACR"),
    "33914-3": (0.0,   200.0, "mL/min/1.73m2", "KDIGO — eGFR"),

    # Cardiovascular / Vitals
    "55284-4": (50.0,  300.0, "mmHg",    "ACC/AHA — systolic BP"),
    "8462-4":  (20.0,  200.0, "mmHg",    "ACC/AHA — diastolic BP"),
    "8867-4":  (20.0,  300.0, "bpm",     "Physiology — heart rate"),
    "8310-5":  (30.0,  45.0,  "°C",      "Clinical — body temperature"),
    "39156-5": (10.0,  100.0, "kg/m2",   "Clinical — BMI"),

    # Lipids
    "2085-9":  (10.0,  200.0, "mg/dL",   "ACC — HDL cholesterol"),
    "13457-7": (10.0,  500.0, "mg/dL",   "ACC — LDL cholesterol"),
    "2571-8":  (30.0,  2000.0, "mg/dL",  "ACC — triglycerides"),

    # Electrolytes
    "2823-3":  (1.0,   10.0,  "mEq/L",   "Clinical — potassium"),
    "2951-2":  (100.0, 180.0, "mEq/L",   "Clinical — sodium"),
    "17861-6": (4.0,   16.0,  "mg/dL",   "Clinical — calcium"),

    # Hematology
    "718-7":   (1.0,   25.0,  "g/dL",    "Clinical — hemoglobin"),
    "4544-3":  (10.0,  75.0,  "%",       "Clinical — hematocrit"),
}

# Name-based fallback (for records without LOINC codes)
NAME_PLAUSIBILITY: dict[str, tuple[float, float]] = {
    "hba1c":         (2.0,   20.0),
    "hemoglobin_a1c": (2.0,  20.0),
    "a1c":           (2.0,   20.0),
    "glucose":       (20.0,  2000.0),
    "creatinine":    (0.1,   30.0),
    "egfr":          (0.0,   200.0),
    "systolic":      (50.0,  300.0),
    "diastolic":     (20.0,  200.0),
    "heart_rate":    (20.0,  300.0),
    "bmi":           (10.0,  100.0),
    "temperature":   (30.0,  45.0),
    "weight":        (0.5,   500.0),
    "potassium":     (1.0,   10.0),
    "sodium":        (100.0, 180.0),
    "hdl":           (10.0,  200.0),
    "ldl":           (10.0,  500.0),
    "triglycerides": (30.0,  2000.0),
}


def validate_plausibility(
    record: dict,
    resource_type: str = "labs",
    patient_mrn: str = "",
) -> dict:
    """
    Apply clinical plausibility rules to a record before Silver write.

    Modifies record in place:
        record["quality_status"]  = "passed" | "flagged"
        record["quality_flags"]   = list of flag dicts
        record["validated_at"]    = ISO timestamp

    Returns the modified record.
    """
    flags: list[dict] = []

    # Extract LOINC code from various possible locations
    loinc = (
        record.get("loinc_code")
        or record.get("code")
        or _extract_from_coding(record)
    )

    # Extract numeric value
    value = _extract_numeric_value(record)

    # LOINC-keyed validation (most precise)
    if loinc and loinc in LOINC_PLAUSIBILITY and value is not None:
        lo, hi, unit, source = LOINC_PLAUSIBILITY[loinc]
        if not (lo <= value <= hi):
            flags.append({
                "type": "loinc_plausibility_fail",
                "loinc": loinc,
                "value": value,
                "acceptable_range": [lo, hi],
                "unit": unit,
                "guideline": source,
                "patient_mrn": patient_mrn,
                "note": (
                    f"Value {value} {unit} is outside acceptable range [{lo}, {hi}]. "
                    f"Possible extraction error (e.g., decimal misplacement)."
                ),
            })

    # Name-based fallback (when no LOINC or LOINC not in our table)
    if not flags and value is not None:
        metric_type = (record.get("metric_type") or record.get("test_name") or "").lower()
        for name_key, (lo, hi) in NAME_PLAUSIBILITY.items():
            if name_key in metric_type:
                if not (lo <= value <= hi):
                    flags.append({
                        "type": "name_plausibility_fail",
                        "metric_name": metric_type,
                        "matched_rule": name_key,
                        "value": value,
                        "acceptable_range": [lo, hi],
                        "patient_mrn": patient_mrn,
                        "note": f"Value {value} outside [{lo}, {hi}] for {name_key}.",
                    })
                break  # First match only

    # Temporal sanity: effective_date can't be in the future
    date_str = (
        record.get("effective_date")
        or record.get("effectiveDateTime")
        or record.get("date")
        or record.get("measured_at")
    )
    if date_str and isinstance(date_str, str):
        try:
            effective = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
            if effective.tzinfo is None:
                effective = effective.replace(tzinfo=timezone.utc)
            if effective > datetime.now(timezone.utc):
                flags.append({
                    "type": "future_date",
                    "field": "effective_date",
                    "value": date_str,
                    "note": "Effective date is in the future — likely extraction error.",
                })
        except (ValueError, TypeError):
            pass

    record["quality_flags"] = flags
    record["quality_status"] = "flagged" if flags else "passed"
    record["validated_at"] = datetime.now(timezone.utc).isoformat()

    if flags:
        log.warning(
            "[PLAUSIBILITY] Flagged record for MRN %s: %s",
            patient_mrn,
            flags[0].get("note", "unknown issue"),
        )

    return record


def _extract_numeric_value(record: dict) -> Optional[float]:
    """Extract the primary numeric value from a record regardless of structure."""
    for key in ("value_quantity", "value", "result_value", "numeric_value",
                "result_numeric", "valueQuantity"):
        val = record.get(key)
        if val is not None:
            if isinstance(val, dict):
                val = val.get("value")
            try:
                return float(val)
            except (ValueError, TypeError):
                continue
    return None


def _extract_from_coding(record: dict) -> Optional[str]:
    """Extract LOINC code from nested FHIR coding structure."""
    code_obj = record.get("code")
    if isinstance(code_obj, dict):
        coding = code_obj.get("coding")
        if isinstance(coding, list) and coding:
            return coding[0].get("code")
    return None
