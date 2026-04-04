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
        "is_synthetic": True,
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
        records.append({
            "id": str(uuid.uuid4()),
            "patient_id": patient_id,
            "code": coding.get("code", ""),
            "display": coding.get("display", ""),
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

        # Extract value
        value = None
        unit = ""
        if "valueQuantity" in r:
            value = r["valueQuantity"].get("value")
            unit = r["valueQuantity"].get("unit", "")
        elif "valueCodeableConcept" in r:
            continue  # Skip non-numeric observations

        if value is None:
            # Try component-based observations (e.g., BP)
            for comp in r.get("component", []):
                comp_coding = comp.get("code", {}).get("coding", [{}])[0]
                comp_value = comp.get("valueQuantity", {})
                if comp_value.get("value") is not None:
                    records.append({
                        "id": str(uuid.uuid4()),
                        "patient_id": patient_id,
                        "metric_type": comp_coding.get("display", "").lower().replace(" ", "_"),
                        "value": float(comp_value["value"]),
                        "unit": comp_value.get("unit", ""),
                        "measured_at": _parse_date(r.get("effectiveDateTime")),
                        "data_source": data_source,
                    })
            continue

        records.append({
            "id": str(uuid.uuid4()),
            "patient_id": patient_id,
            "metric_type": coding.get("display", "").lower().replace(" ", "_"),
            "value": float(value),
            "unit": unit,
            "measured_at": _parse_date(r.get("effectiveDateTime")),
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
        if r.get("type"):
            type_list = r["type"][0] if r["type"] else {}
            type_coding = (
                type_list.get("coding", [{}])[0]
                if type_list.get("coding")
                else {}
            )

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
        transformed = fn(resource, patient_id)
        if isinstance(transformed, list):
            results.extend(transformed)
        elif transformed:
            results.append(transformed)
    for rec in results:
        rec["data_source"] = source
    return results
