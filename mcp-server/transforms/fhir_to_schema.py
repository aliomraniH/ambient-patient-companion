"""Pure transformation functions: FHIR resources → DB table records.

No database calls. No print(). All functions return dicts
matching the target table columns, including data_source.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any


def _safe_str(value: Any, default: str = "") -> str:
    return str(value) if value is not None else default


def _parse_date(date_str: str | None) -> date | None:
    if not date_str:
        return None
    try:
        return datetime.fromisoformat(date_str.replace("Z", "+00:00")).date()
    except (ValueError, AttributeError):
        try:
            return date.fromisoformat(date_str[:10])
        except (ValueError, AttributeError):
            return None


def transform_patient(
    patient_resource: dict[str, Any],
    data_source: str = "synthea",
    is_synthetic: bool = True,
) -> dict[str, Any]:
    """Transform a FHIR Patient resource into a patients table record."""
    name = {}
    if patient_resource.get("name"):
        name = patient_resource["name"][0]

    address = {}
    if patient_resource.get("address"):
        address = patient_resource["address"][0]

    # Race and ethnicity from US Core extensions
    race = ""
    ethnicity = ""
    for ext in patient_resource.get("extension", []):
        url = ext.get("url", "")
        if "us-core-race" in url:
            for sub_ext in ext.get("extension", []):
                if sub_ext.get("url") == "text":
                    race = _safe_str(sub_ext.get("valueString"))
        elif "us-core-ethnicity" in url:
            for sub_ext in ext.get("extension", []):
                if sub_ext.get("url") == "text":
                    ethnicity = _safe_str(sub_ext.get("valueString"))

    # MRN from identifiers
    mrn = ""
    for identifier in patient_resource.get("identifier", []):
        id_type = identifier.get("type", {})
        for coding in id_type.get("coding", []):
            if coding.get("code") == "MR":
                mrn = _safe_str(identifier.get("value"))
                break
        if mrn:
            break

    if not mrn:
        mrn = f"SYN-{uuid.uuid4().hex[:8].upper()}"

    return {
        "id": str(uuid.uuid4()),
        "mrn": mrn,
        "first_name": _safe_str(
            name.get("given", [""])[0] if name.get("given") else ""
        ),
        "last_name": _safe_str(name.get("family", "")),
        "birth_date": _parse_date(patient_resource.get("birthDate")),
        "gender": _safe_str(patient_resource.get("gender")),
        "race": race,
        "ethnicity": ethnicity,
        "address_line": " ".join(address.get("line", [])),
        "city": _safe_str(address.get("city")),
        "state": _safe_str(address.get("state")),
        "zip_code": _safe_str(address.get("postalCode")),
        "is_synthetic": is_synthetic,
        "data_source": data_source,
    }


def transform_conditions(
    condition_resources: list[dict[str, Any]],
    patient_id: str,
    data_source: str = "synthea",
) -> list[dict[str, Any]]:
    """Transform FHIR Condition resources into patient_conditions records."""
    records = []
    for r in condition_resources:
        code_obj = r.get("code", {})
        coding = code_obj.get("coding", [{}])[0] if code_obj.get("coding") else {}
        # Prefer coding.display; fall back to code.text when coding block is absent
        display = coding.get("display", "") or code_obj.get("text", "")
        records.append({
            "id": str(uuid.uuid4()),
            "patient_id": patient_id,
            "code": coding.get("code", ""),
            "display": display,
            "system": coding.get("system", ""),
            "onset_date": _parse_date(r.get("onsetDateTime")),
            "clinical_status": (
                r.get("clinicalStatus", {})
                .get("coding", [{}])[0]
                .get("code", "")
            ),
            "data_source": data_source,
        })
    return records


def transform_medications(
    medication_resources: list[dict[str, Any]],
    patient_id: str,
    data_source: str = "synthea",
) -> list[dict[str, Any]]:
    """Transform FHIR MedicationRequest resources into patient_medications records."""
    records = []
    for r in medication_resources:
        med_code = r.get("medicationCodeableConcept", {})
        coding = med_code.get("coding", [{}])[0] if med_code.get("coding") else {}
        records.append({
            "id": str(uuid.uuid4()),
            "patient_id": patient_id,
            "code": coding.get("code", ""),
            "display": coding.get("display", ""),
            "system": coding.get("system", ""),
            "status": r.get("status", "active"),
            "authored_on": _parse_date(r.get("authoredOn")),
            "data_source": data_source,
        })
    return records


def _extract_loinc_code(code_obj: dict[str, Any]) -> str:
    """Extract LOINC code from a FHIR CodeableConcept's coding array."""
    for coding in code_obj.get("coding", []):
        system = coding.get("system", "")
        if "loinc" in system.lower():
            return coding.get("code", "")
    return ""


def _extract_reference_range(observation: dict[str, Any]) -> tuple[str, float | None, float | None]:
    """Extract reference range text and bounds from FHIR Observation.referenceRange."""
    ref_ranges = observation.get("referenceRange", [])
    if not ref_ranges:
        # Check pass-through field from HealthEx converters
        ref_text = observation.get("_reference_text", "")
        return ref_text, None, None

    ref = ref_ranges[0]
    low_val = ref.get("low", {}).get("value")
    high_val = ref.get("high", {}).get("value")
    text = ref.get("text", "")

    if not text:
        parts = []
        if low_val is not None:
            parts.append(str(low_val))
        if high_val is not None:
            parts.append(str(high_val))
        if parts:
            text = " - ".join(parts)
            low_unit = ref.get("low", {}).get("unit", "") or ref.get("high", {}).get("unit", "")
            if low_unit:
                text += f" {low_unit}"

    return text, low_val, high_val


def transform_clinical_observations(
    observation_resources: list[dict[str, Any]],
    patient_id: str,
    data_source: str = "synthea",
) -> list[dict[str, Any]]:
    """Transform FHIR Observation resources into biometric_readings records."""
    records = []
    for r in observation_resources:
        code_obj = r.get("code", {})
        coding = code_obj.get("coding", [{}])[0] if code_obj.get("coding") else {}

        # Extract LOINC code from coding system or pass-through field
        loinc_code = _extract_loinc_code(code_obj) or r.get("_loinc_code", "")

        # Extract reference range
        ref_text, ref_low, ref_high = _extract_reference_range(r)

        # Extract value
        value = None
        unit = ""
        result_text = r.get("_result_text")  # pass-through from HealthEx converter
        if "valueQuantity" in r:
            value = r["valueQuantity"].get("value")
            unit = r["valueQuantity"].get("unit", "")
        elif "valueCodeableConcept" in r:
            # Qualitative result — extract display text
            val_coding = r["valueCodeableConcept"].get("coding", [{}])
            result_text = val_coding[0].get("display", "") if val_coding else ""
            value = 0.0  # placeholder for backward compat

        if value is None and not result_text:
            # Try component-based observations (e.g., BP)
            for comp in r.get("component", []):
                comp_code_obj = comp.get("code", {})
                comp_coding = comp_code_obj.get("coding", [{}])[0] if comp_code_obj.get("coding") else {}
                # Prefer coding.display; fall back to code.text
                comp_display = comp_coding.get("display", "") or comp_code_obj.get("text", "")
                comp_value = comp.get("valueQuantity", {})
                comp_loinc = _extract_loinc_code(comp_code_obj)
                if comp_value.get("value") is not None:
                    comp_unit = comp_value.get("unit", "")
                    comp_numeric = float(comp_value["value"])
                    records.append({
                        "id": str(uuid.uuid4()),
                        "patient_id": patient_id,
                        "metric_type": comp_display.lower().replace(" ", "_"),
                        "value": comp_numeric,
                        "unit": comp_unit,
                        "measured_at": _parse_date(r.get("effectiveDateTime")),
                        "result_numeric": comp_numeric,
                        "result_unit": comp_unit,
                        "loinc_code": comp_loinc or None,
                        "data_source": data_source,
                    })
            continue

        if value is None and result_text:
            value = 0.0  # placeholder for backward compat with NOT NULL constraint

        # Prefer coding.display; fall back to code.text when coding block is absent
        metric_display = coding.get("display", "") or code_obj.get("text", "")
        numeric_val = float(value)
        records.append({
            "id": str(uuid.uuid4()),
            "patient_id": patient_id,
            "metric_type": metric_display.lower().replace(" ", "_"),
            "value": numeric_val,
            "unit": unit,
            "measured_at": _parse_date(r.get("effectiveDateTime")),
            # New structured fields
            "result_text": result_text,
            "result_numeric": numeric_val if not result_text else None,
            "result_unit": unit,
            "reference_text": ref_text or None,
            "reference_low": ref_low,
            "reference_high": ref_high,
            "loinc_code": loinc_code or None,
            "data_source": data_source,
        })
    return records


def transform_encounters(
    encounter_resources: list[dict[str, Any]],
    patient_id: str,
    data_source: str = "synthea",
) -> list[dict[str, Any]]:
    """Transform FHIR Encounter resources into clinical_events records."""
    records = []
    for r in encounter_resources:
        type_coding = {}
        raw_type = r.get("type")
        if raw_type and isinstance(raw_type, list):
            type_list = raw_type[0] if raw_type else {}
            if isinstance(type_list, dict):
                type_coding = (
                    type_list.get("coding", [{}])[0]
                    if type_list.get("coding")
                    else {}
                )
        elif raw_type and isinstance(raw_type, str):
            type_coding = {"display": raw_type}

        period = r.get("period", {})
        event_date = _parse_date(period.get("start"))

        records.append({
            "id": str(uuid.uuid4()),
            "patient_id": patient_id,
            "event_type": type_coding.get("display", _safe_str(r.get("class", {}).get("code"))),
            "event_date": event_date,
            "description": type_coding.get("display", ""),
            "source_system": "FHIR Encounter",
            "data_source": data_source,
        })
    return records


def transform_wearable_data(
    wearable_data: list[dict[str, Any]],
    patient_id: str,
    data_source: str = "synthea",
) -> list[dict[str, Any]]:
    """Transform wearable device data into biometric_readings records."""
    records = []
    for w in wearable_data:
        if w.get("type") in ("vitals_placeholder",):
            continue  # Skip placeholder entries
        records.append({
            "id": str(uuid.uuid4()),
            "patient_id": patient_id,
            "metric_type": w.get("metric_type", "unknown"),
            "value": float(w.get("value", 0)),
            "unit": w.get("unit", ""),
            "measured_at": w.get("measured_at"),
            "device_source": w.get("device_source", ""),
            "data_source": data_source,
        })
    return records


def transform_by_type(
    resource_type: str,
    resources: list,
    patient_id: str,
    source: str,
) -> list:
    """Route HealthEx resource lists to the correct transform function.

    Args:
        resource_type: "labs" | "medications" | "conditions" |
                       "encounters" | "summary"
        resources: list of FHIR resource dicts from HealthEx response
        patient_id: UUID of the patient in the database
        source: data_source tag to apply to all output rows
    """
    mapping = {
        "labs":        transform_clinical_observations,
        "medications": transform_medications,
        "conditions":  transform_conditions,
        "encounters":  transform_encounters,
        "summary":     transform_patient,
    }
    fn = mapping.get(resource_type)
    if not fn:
        raise ValueError(
            f"Unknown resource_type: '{resource_type}'. "
            f"Must be one of: {list(mapping.keys())}"
        )
    results = []
    for resource in resources:
        if fn is transform_patient:
            # transform_patient takes (resource, data_source) — no patient_id
            transformed = fn(resource)
        else:
            # All other transforms take (resource_list, patient_id, data_source)
            transformed = fn([resource], patient_id, source)
        if isinstance(transformed, list):
            results.extend(transformed)
        elif transformed:
            results.append(transformed)
    for rec in results:
        rec["data_source"] = source
    return results
